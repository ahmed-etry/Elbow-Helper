"""Reply targets are readable, previewed and checked again before posting."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from features.agent.discord_actions import test_agent_discord_messages as fixtures
from elbow_helper.features.agent.discord_actions.messages import prepare_post


class PostReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.DiscordMessageActionTests.asyncSetUp(self)
        self.context.guild.get_member = lambda _: self.context.member
        self.channel.permissions_for = lambda _: SimpleNamespace(
            view_channel=True, send_messages=True, read_message_history=True,
        )
        self.target = SimpleNamespace(
            id=55, channel=self.channel,
            author=SimpleNamespace(id=7, display_name="Example", name="example.7"),
            content="Synthetic question", jump_url="https://discord.com/channels/3/2/55",
        )
        self.messages[55] = self.target
        self.context.guild.members = [self.target.author, self.context.member]
        self.context.state.request_text = "Reply to <@7>."
        self.context.source_message.raw_mentions = [7]
        self.guard = patch(
            "elbow_helper.features.agent.discord_actions.messages.require_evidence_access",
            new_callable=AsyncMock,
        )
        self.guard.start()
        self.addCleanup(self.guard.stop)

    async def test_reply_preview_and_no_author_ping_by_default(self):
        await prepare_post(self.context, {"channel_id": 2, "text": "Answer", "reply_to": 55})
        action = self.context.state.proposed_changes.pop()
        self.assertEqual(action.preview.details[0],
                         "Reply to Example, no ping: [Synthetic question]"
                         "(https://discord.com/channels/3/2/55)")
        self.assertEqual(action.preview.detail_sources, frozenset({2}))
        await action.run()
        options = self.channel.send.await_args.kwargs
        self.assertEqual(options["reference"].message_id, 55)
        self.assertFalse(options["allowed_mentions"].replied_user)

    async def test_explicit_author_ping_and_only_first_chunk_replies(self):
        await prepare_post(self.context, {
            "channel_id": 2, "text": "word " * 500, "reply_to": 55, "ping_author": True,
        })
        for action in self.context.state.proposed_changes:
            await action.run()
        self.assertIn(
            "Reply to Example, with ping: [Synthetic question]"
            "(https://discord.com/channels/3/2/55)",
            self.context.state.proposed_changes[0].preview.details,
        )
        first, second = self.channel.send.await_args_list
        self.assertTrue(first.kwargs["allowed_mentions"].replied_user)
        self.assertNotIn("reference", second.kwargs)

    async def test_unreadable_or_wrong_channel_target_is_refused(self):
        self.channel.permissions_for = lambda _: SimpleNamespace(
            view_channel=True, send_messages=True, read_message_history=False,
        )
        result = await prepare_post(self.context, {
            "channel_id": 2, "text": "Answer", "reply_to": 55,
        })
        self.assertIn("error", result)
        self.channel.permissions_for = lambda _: SimpleNamespace(
            view_channel=True, send_messages=True, read_message_history=True,
        )
        self.target.channel = SimpleNamespace(id=99)
        result = await prepare_post(self.context, {
            "channel_id": 2, "text": "Answer", "reply_to": 55,
        })
        self.assertIn("error", result)
        self.assertEqual(self.context.state.proposed_changes, [])

    async def test_changed_target_refuses_confirmation(self):
        await prepare_post(self.context, {"channel_id": 2, "text": "Answer", "reply_to": 55})
        action = self.context.state.proposed_changes.pop()
        self.target.content = "Changed question"
        self.assertFalse(await action.preview.recheck())
        self.channel.send.assert_not_awaited()
