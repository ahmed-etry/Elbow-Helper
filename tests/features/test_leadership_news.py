from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from discord.ext import commands

from elbow_helper.configuration.channels import LEAD_NEWS, PUBLIC_NEWS
from elbow_helper.configuration.roles import LEAD
from elbow_helper.features.leadership_news.cog import LeadNews
from elbow_helper.infrastructure.persistence import read_json, write_json_atomic


class LeadNewsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = TemporaryDirectory(dir=Path(__file__).parents[2])
        self.addCleanup(self.directory.cleanup)
        self.state_file = Path(self.directory.name) / "state.json"
        state_patch = patch(
            "elbow_helper.features.leadership_news.cog.STATE_FILE", self.state_file,
        )
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        self.bot._connection.user = SimpleNamespace(id=900, bot=True)
        self.guild = SimpleNamespace(id=100)
        self.bot._connection._guilds[self.guild.id] = self.guild
        self.bot.wait_until_ready = AsyncMock()
        self.lead_channel = MagicMock(spec=discord.TextChannel)
        self.lead_channel.id = LEAD_NEWS
        self.lead_channel.guild = self.guild
        self.target = MagicMock(spec=discord.TextChannel)
        self.target.id = PUBLIC_NEWS
        self.target.guild = self.guild
        self.sent = SimpleNamespace(
            id=101,
            jump_url=f"https://discord.com/channels/{self.guild.id}/{PUBLIC_NEWS}/101",
        )
        self.target.send = AsyncMock(return_value=self.sent)
        self.bot.get_channel = MagicMock(side_effect=lambda channel_id: {
            LEAD_NEWS: self.lead_channel, PUBLIC_NEWS: self.target,
        }.get(channel_id))
        self.prompt = SimpleNamespace(
            id=8, channel=self.lead_channel, author=self.bot.user,
            reference=SimpleNamespace(message_id=7),
            content=f"Publish this update to <#{PUBLIC_NEWS}>?",
            delete=AsyncMock(),
        )
        self.source = SimpleNamespace(
            id=7, channel=self.lead_channel, guild=self.guild,
            content="A synthetic update", attachments=[],
            author=SimpleNamespace(bot=False),
            reply=AsyncMock(return_value=self.prompt),
        )
        self.lead_channel.fetch_message = AsyncMock(return_value=self.source)
        self.lead_channel.get_partial_message.return_value = self.prompt
        self.cogs = []
        self.cog = self.new_cog()
        self.bot.get_cog = MagicMock(side_effect=lambda _: self.cogs[-1])

    async def asyncTearDown(self) -> None:
        for cog in self.cogs:
            await self.stop_cog(cog)
        await self.bot.close()

    def new_cog(self) -> LeadNews:
        cog = LeadNews(self.bot)
        self.cogs.append(cog)
        return cog

    @staticmethod
    async def stop_cog(cog: LeadNews) -> None:
        tasks = list(cog._cleanup_tasks.values())
        cog.cog_unload()
        await asyncio.gather(*tasks, return_exceptions=True)

    def interaction(self) -> SimpleNamespace:
        return SimpleNamespace(
            user=SimpleNamespace(roles=[SimpleNamespace(id=next(iter(LEAD)))]),
            channel=self.lead_channel, client=self.bot, message=self.prompt,
            response=SimpleNamespace(send_message=AsyncMock()),
        )

    @staticmethod
    def http_error(status: int) -> discord.HTTPException:
        response = SimpleNamespace(status=status, reason="Synthetic failure")
        error = discord.NotFound if status == 404 else discord.HTTPException
        return error(response, "Synthetic failure")

    async def test_concurrent_publish_buttons_send_once(self) -> None:
        await self.cog.on_message(self.source)
        view = self.cog._prompt_views[self.prompt.id]
        started = asyncio.Event()
        release = asyncio.Event()

        async def send(**kwargs):
            started.set()
            await release.wait()
            return self.sent

        self.target.send.side_effect = send
        interactions = (self.interaction(), self.interaction())
        first = asyncio.create_task(view.publish_btn.callback(interactions[0]))
        await asyncio.wait_for(started.wait(), timeout=1)
        second = asyncio.create_task(view.publish_btn.callback(interactions[1]))
        try:
            await asyncio.sleep(0)
            self.assertEqual(self.target.send.await_count, 1)
        finally:
            release.set()
            await asyncio.gather(first, second)

        self.target.send.assert_awaited_once()
        self.assertEqual(read_json(self.state_file)["published"]["7"]["message_id"], 101)
        self.assertEqual(read_json(self.state_file)["prompts"], {})
        for interaction in interactions:
            interaction.response.send_message.assert_awaited_once_with(
                f"Published to <#{PUBLIC_NEWS}>", ephemeral=True,
            )

    async def test_published_source_remains_idempotent_after_restart(self) -> None:
        await self.cog.publish_public_news(self.source, self.target)
        restarted = self.new_cog()

        repeated = await restarted.publish_public_news(self.source, self.target)

        self.target.send.assert_awaited_once()
        self.assertEqual(repeated.id, self.sent.id)
        self.assertEqual(repeated.jump_url, self.sent.jump_url)
        await restarted.on_message(self.source)
        self.source.reply.assert_not_awaited()

    async def test_send_failure_can_be_retried(self) -> None:
        self.target.send.side_effect = self.http_error(503)
        with self.assertRaises(discord.HTTPException):
            await self.cog.publish_public_news(self.source, self.target)
        self.assertEqual(self.cog._state["published"], {})

        self.target.send.side_effect = None
        await self.cog.publish_public_news(self.source, self.target)

        self.assertEqual(self.target.send.await_count, 2)
        self.assertEqual(read_json(self.state_file)["published"]["7"]["message_id"], 101)

    async def test_different_sources_can_publish_concurrently(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def send(*, content, **kwargs):
            if content == self.source.content:
                started.set()
                await release.wait()
            return self.sent

        self.target.send.side_effect = send
        first = asyncio.create_task(self.cog.publish_public_news(self.source, self.target))
        await asyncio.wait_for(started.wait(), timeout=1)
        other = SimpleNamespace(id=9, content="Another synthetic update", attachments=[])
        try:
            await asyncio.wait_for(self.cog.publish_public_news(other, self.target), timeout=1)
        finally:
            release.set()
            await first

        self.assertEqual(set(read_json(self.state_file)["published"]), {"7", "9"})

    async def test_receipt_is_saved_before_prompt_cleanup_and_survives_its_failure(self) -> None:
        async def failed_cleanup():
            self.assertEqual(read_json(self.state_file)["published"]["7"]["message_id"], 101)
            raise self.http_error(503)

        await self.cog.on_message(self.source)
        self.prompt.delete.side_effect = failed_cleanup
        await self.cog.publish_public_news(self.source, self.target, prompts=(self.prompt,))
        self.assertIn("8", read_json(self.state_file)["prompts"])
        await self.stop_cog(self.cog)
        restarted = self.new_cog()
        self.prompt.delete.side_effect = None

        await restarted.publish_public_news(self.source, self.target, prompts=(self.prompt,))

        self.target.send.assert_awaited_once()
        self.assertEqual(read_json(self.state_file)["prompts"], {})

    async def test_cancelled_cleanup_does_not_lose_publication_receipt(self) -> None:
        self.prompt.delete.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.cog.publish_public_news(self.source, self.target, prompts=(self.prompt,))

        restarted = self.new_cog()
        await restarted.publish_public_news(self.source, self.target)

        self.target.send.assert_awaited_once()

    async def test_prompt_view_and_original_expiry_are_restored(self) -> None:
        with patch("elbow_helper.features.leadership_news.cog.time.time", return_value=1000):
            await self.cog.on_message(self.source)
        stored = read_json(self.state_file)
        self.assertEqual(stored["prompts"]["8"], {
            "source_message_id": 7, "channel_id": LEAD_NEWS, "expires_at": 87400,
        })
        self.assertIs(self.source.reply.await_args.kwargs["view"], self.cog._prompt_views[8])
        await self.stop_cog(self.cog)
        restarted = self.new_cog()
        sleeping = asyncio.Event()
        delays = []

        async def sleep(delay):
            delays.append(delay)
            sleeping.set()
            await asyncio.Event().wait()

        with (
            patch("elbow_helper.features.leadership_news.cog.time.time", return_value=82000),
            patch("elbow_helper.features.leadership_news.cog.asyncio.sleep", side_effect=sleep),
        ):
            await restarted.cog_load()
            await asyncio.wait_for(sleeping.wait(), timeout=1)
            self.assertEqual(delays, [5400])
            view = restarted._prompt_views[8]
            self.assertIn(view, self.bot.persistent_views)
            self.assertEqual((view.source_message_id, view.prompt_message_id,
                              view.prompt_channel_id), (7, 8, LEAD_NEWS))
            self.assertEqual({child.custom_id for child in view.children},
                             {"lead_news:publish", "lead_news:dismiss"})
            await view.publish_btn.callback(self.interaction())

        self.target.send.assert_awaited_once()
        self.assertEqual(read_json(self.state_file)["prompts"], {})

    async def test_expired_prompt_is_cleaned_up_after_restart(self) -> None:
        with patch("elbow_helper.features.leadership_news.cog.time.time", return_value=0):
            await self.cog.on_message(self.source)
        await self.stop_cog(self.cog)
        restarted = self.new_cog()

        await restarted.cog_load()
        await asyncio.wait_for(asyncio.gather(*restarted._cleanup_tasks.values()), timeout=1)

        self.prompt.delete.assert_awaited_once()
        self.assertEqual(read_json(self.state_file)["prompts"], {})
        self.assertFalse(restarted._prompt_views)
        self.target.send.assert_not_awaited()

    async def test_cleanup_retries_transient_discord_failure(self) -> None:
        with patch("elbow_helper.features.leadership_news.cog.time.time", return_value=0):
            await self.cog.on_message(self.source)
        await self.stop_cog(self.cog)
        restarted = self.new_cog()
        self.prompt.delete.side_effect = [self.http_error(503), None]

        with patch("elbow_helper.features.leadership_news.cog.asyncio.sleep", new_callable=AsyncMock) as sleep:
            await restarted.cog_load()
            await asyncio.wait_for(asyncio.gather(*restarted._cleanup_tasks.values()), timeout=1)

        self.assertEqual(self.prompt.delete.await_count, 2)
        self.assertEqual(sleep.await_args_list[1].args, (60,))
        self.assertEqual(read_json(self.state_file)["prompts"], {})

    async def test_dismissed_or_missing_prompts_are_not_restored(self) -> None:
        for missing in (False, True):
            with self.subTest(missing=missing):
                await self.cog.on_message(self.source)
                self.prompt.delete.side_effect = self.http_error(404) if missing else None

                self.assertTrue(await self.cog.dismiss_public_news_prompt(self.prompt))

                restarted = self.new_cog()
                await restarted.cog_load()
                self.assertFalse(restarted._prompt_views)
                self.assertEqual(read_json(self.state_file)["prompts"], {})

    async def test_unload_cancels_cleanup_and_unregisters_view_without_losing_prompt(self) -> None:
        await self.cog.on_message(self.source)
        task = self.cog._cleanup_tasks[8]
        view = self.cog._prompt_views[8]

        await self.stop_cog(self.cog)

        self.assertTrue(task.cancelled())
        self.assertNotIn(view, self.bot.persistent_views)
        self.assertIn("8", read_json(self.state_file)["prompts"])

    async def test_corrupt_state_does_not_silently_allow_duplicate_publication(self) -> None:
        self.state_file.write_text("{", encoding="utf-8")

        with self.assertRaises(ValueError):
            self.new_cog()

        self.target.send.assert_not_awaited()

    async def test_expired_missing_prompt_is_forgotten(self) -> None:
        write_json_atomic(self.state_file, {
            "published": {}, "prompts": {
                "8": {"source_message_id": 7, "channel_id": LEAD_NEWS, "expires_at": 0},
            },
        })
        restarted = self.new_cog()
        self.prompt.delete.side_effect = self.http_error(404)

        await restarted.cog_load()
        await asyncio.wait_for(asyncio.gather(*restarted._cleanup_tasks.values()), timeout=1)

        self.assertEqual(read_json(self.state_file)["prompts"], {})


if __name__ == "__main__":
    unittest.main()
