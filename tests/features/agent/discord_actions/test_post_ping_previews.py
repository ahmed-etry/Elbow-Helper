"""Preview every member ping, including an explicitly pinged reply author."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from features.agent.discord_actions import test_agent_discord_messages as fixtures
from elbow_helper.features.agent.discord_actions.messages import prepare_edit, prepare_post


class PostPingPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.DiscordMessageActionTests.asyncSetUp(self)
        self.channel.permissions_for = lambda _: SimpleNamespace(
            view_channel=True, send_messages=True, read_message_history=True,
        )
        self.messages[55] = SimpleNamespace(
            id=55, channel=self.channel, author=self.context.guild.get_member(7),
            content="Synthetic question", jump_url="https://discord.com/channels/3/2/55",
        )
        self.guard = patch(
            "elbow_helper.features.agent.discord_actions.messages.require_evidence_access",
            new_callable=AsyncMock,
        )
        self.guard.start()
        self.addCleanup(self.guard.stop)

    async def preview(self, **arguments):
        await prepare_post(self.context, {"channel_id": 2, **arguments})
        return self.context.state.proposed_changes.pop().preview

    async def test_member_mentions_are_listed_once_with_both_mention_forms(self):
        preview = await self.preview(text="Hello <@7> <@!8> <@7>")
        self.assertIn("Pings: Example (@example.7), Example (@example.8)", preview.lines)

    async def test_author_is_listed_only_when_pinged_and_never_duplicated(self):
        plain = await self.preview(text="Answer", reply_to=55)
        self.assertFalse(any(line.startswith("Pings:") for line in plain.lines))
        self.assertIn("Reply to Example (@example.7): [Synthetic question]"
                      "(https://discord.com/channels/3/2/55)", plain.details)
        pinged = await self.preview(text="Answer", reply_to=55, ping_author=True)
        self.assertIn("Pings: Example (@example.7)", pinged.lines)
        duplicate = await self.preview(text="Answer <@!7>", reply_to=55, ping_author=True)
        self.assertIn("Pings: Example (@example.7)", duplicate.lines)

    async def test_everyone_roles_and_members_are_all_listed(self):
        self.context.guild.get_role = lambda _: SimpleNamespace(id=9, mention="<@&9>")
        preview = await self.preview(
            text="@here <@&9> <@7>", ping_everyone=True, ping_role_ids=[9],
        )
        self.assertIn("Pings: @everyone or @here, <@&9>, Example (@example.7)", preview.lines)

    async def test_unselected_role_and_everyone_mentions_are_not_claimed_as_pings(self):
        preview = await self.preview(text="@here <@&9> <@7>")
        self.assertIn("Pings: Example (@example.7)", preview.lines)

    async def test_gif_only_reply_never_claims_an_author_ping(self):
        preview = await self.preview(
            text="https://tenor.com/view/synthetic", reply_to=55, ping_author=True,
        )
        self.assertFalse(any(line.startswith("Pings:") for line in preview.lines))

    async def test_edit_previews_both_members_with_the_same_display_name(self):
        await prepare_post(self.context, {"channel_id": 2, "text": "Before"})
        outcome = await self.context.state.proposed_changes.pop().run()
        await prepare_edit(self.context, {
            "channel_id": 2, "message_id": outcome.result["message_id"],
            "text": "Hello <@7> <@8>",
        })
        preview = self.context.state.proposed_changes.pop().preview
        self.assertIn("Pings: Example (@example.7), Example (@example.8)", preview.lines)

    async def test_uncached_mention_is_resolved_before_preview(self):
        self.context.guild.fetch_member = AsyncMock(return_value=SimpleNamespace(
            id=10, display_name="Example", name="example.10",
        ))
        preview = await self.preview(text="Hello <@10>")
        self.context.guild.fetch_member.assert_awaited_once_with(10)
        self.assertIn("Pings: Example (@example.10)", preview.lines)
