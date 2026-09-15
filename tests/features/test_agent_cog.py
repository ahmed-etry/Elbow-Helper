from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timezone
import asyncio
import discord
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.cog import CoreAgent
from elbow_helper.features.agent.message_content import message_text


class _Member:
    def __init__(self, member_id: int, role_ids: tuple[int, ...]):
        self.id = member_id
        self.bot = False
        self.display_name = f"Member {member_id}"
        self.roles = [SimpleNamespace(id=role_id) for role_id in role_ids]


def _message(*, author: _Member, bot_id: int, content: str):
    return SimpleNamespace(
        author=author,
        guild=SimpleNamespace(id=GUILD_ID),
        raw_mentions=[bot_id],
        content=content,
    )


class CoreAgentCogTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_includes_waiting_for_capacity(self):
        self.cog._semaphore = asyncio.Semaphore(0)
        self.cog._send_failure = AsyncMock()
        member = _Member(42, (next(iter(CORE)),))
        message = _message(author=member, bot_id=999, content="<@999> hello")
        with (
            patch("elbow_helper.features.agent.cog.discord.Member", _Member),
            patch("elbow_helper.features.agent.cog.AGENT_REQUEST_TIMEOUT_SECONDS", 0.01),
        ):
            await self.cog.on_message(message)
        self.cog._answer.assert_not_awaited()
        self.cog._send_failure.assert_awaited_once()
        self.assertFalse(self.cog._active_members)
        self.assertFalse(self.cog._tasks)

    def test_embed_content_is_available_as_evidence(self):
        embed = discord.Embed(title="Applicant Review", description="Discussion")
        embed.add_field(name="Player", value="Example")
        text = message_text(SimpleNamespace(content="", embeds=[embed], attachments=[]))
        self.assertIn("Applicant Review", text)
        self.assertIn("Player: Example", text)

    async def test_local_context_uses_nearest_messages_in_chronological_order(self):
        items = [SimpleNamespace(
            id=i, content=f"text-{i}", attachments=[], embeds=[],
            author=SimpleNamespace(id=42, bot=False, display_name="Member"),
            created_at=datetime.now(timezone.utc), jump_url=f"source/{i}",
        ) for i in range(20)]
        async def history(*, limit, before, oldest_first):
            selected = items[:limit] if oldest_first else list(reversed(items))[:limit]
            for item in selected:
                yield item
        message = SimpleNamespace(channel=SimpleNamespace(id=100, history=history), mentions=[])
        result = await self.cog._build_local_context(message, None)
        self.assertIn("text-12", result)
        self.assertIn("text-19", result)
        self.assertNotIn("text-0", result)
        self.assertLess(result.index("text-12"), result.index("text-19"))

    async def test_long_reply_preserves_complete_answer_as_attachment(self):
        message = SimpleNamespace(mentions=[], reply=AsyncMock())
        response = "answer " * 3000
        await self.cog._send_response(message, response, None)
        attached = message.reply.await_args.kwargs["file"]
        self.assertEqual(attached.fp.read().decode("utf-8"), response)
        attached.close()

    def setUp(self) -> None:
        self.bot = SimpleNamespace(
            user=SimpleNamespace(id=999),
            agent_model=MagicMock(),
        )
        self.cog = CoreAgent(
            self.bot,
            account_links=object(),
            clan_health=object(),
            message_search=object(),
        )
        self.cog._answer = AsyncMock()

    async def test_core_mention_starts_agent_with_clean_question(self) -> None:
        member = _Member(42, (next(iter(CORE)),))
        message = _message(
            author=member,
            bot_id=999,
            content="<@999> tell this guy to piss off",
        )

        with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
            await self.cog.on_message(message)

        self.cog._answer.assert_awaited_once_with(
            message,
            member,
            "tell this guy to piss off",
        )

    async def test_non_core_mention_is_silently_ignored(self) -> None:
        member = _Member(42, ())
        message = _message(
            author=member,
            bot_id=999,
            content="<@999> hello",
        )

        with patch("elbow_helper.features.agent.cog.discord.Member", _Member):
            await self.cog.on_message(message)

        self.cog._answer.assert_not_awaited()

    def test_message_without_direct_bot_mention_is_ignored(self) -> None:
        member = _Member(42, (next(iter(CORE)),))
        message = SimpleNamespace(
            author=member,
            guild=SimpleNamespace(id=GUILD_ID),
            raw_mentions=[],
            content="talking about the bot",
        )

        self.assertFalse(self.cog._is_agent_request(message))


if __name__ == "__main__":
    unittest.main()
