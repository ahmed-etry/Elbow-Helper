from tests.agent.engine.helpers import LegacyCatalogueMixin

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.discord_actions.messages import prepare_post
from . import test_agent_discord_messages as fixtures


class CurrentChannelPostTests(LegacyCatalogueMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.DiscordMessageActionTests.asyncSetUp(self)
        self.context.source_message.channel = self.channel
        self.context.delivery_channel = None
        self.target = SimpleNamespace(
            id=55, channel=self.channel, content="Synthetic question",
            jump_url="https://discord.com/channels/3/2/55",
            author=SimpleNamespace(id=7, display_name="Synthetic member", name="synthetic.7"),
        )
        self.messages[55] = self.target
        self.context.state.request_text = "Reply to <@7>."
        self.context.source_message.raw_mentions = [7]
        self.channel.permissions_for = lambda _: SimpleNamespace(
            view_channel=True, send_messages=True, read_message_history=True,
        )
        self.context.send_reply = AsyncMock(return_value={"message_ids": [200], "channel_id": 2})
        guard = patch(
            "elbow_helper.agent.discord_actions.messages.require_evidence_access",
            new_callable=AsyncMock,
        )
        guard.start()
        self.addCleanup(guard.stop)

    async def test_current_channel_posts_send_instead_of_preparing_a_change(self):
        for options in ({}, {"reply_to": 55}, {"reply_to": 55, "ping_author": True}):
            with self.subTest(options=options):
                with self.assertLogs(
                    "elbow_helper.agent.discord_actions.messages", level="INFO",
                ) as logs:
                    result = await prepare_post(self.context, {
                        "channel_id": 2, "text": "Synthetic answer", **options,
                    })
                self.assertTrue(result["delivered_as_reply"])
                self.assertEqual(result["status"], "complete")
                self.assertEqual(result["message_ids"], [200])
                self.context.send_reply.assert_awaited_with(self.context, "Synthetic answer")
                self.assertEqual(self.context.state.reply_to, options.get("reply_to"))
                self.assertEqual(self.context.state.ping_author, options.get("ping_author", False))
                self.assertEqual(self.context.state.proposed_changes, [])
                self.assertNotIn("Synthetic answer", "\n".join(logs.output))
                self.assertIn("messages=[200]", "\n".join(logs.output))

    async def test_unknown_source_reference_is_dropped_without_losing_the_answer(self):
        result = await prepare_post(self.context, {
            "channel_id": 2, "text": "My recommendation: keep them. [[message:999]]",
        })
        self.assertTrue(result["delivered_as_reply"])
        self.context.send_reply.assert_awaited_once_with(self.context, "My recommendation: keep them.")
        self.assertEqual(len(self.context.state.reply_delivery.posts), 1)


    async def test_other_channels_broadcasts_roles_and_files_still_need_confirmation(self):
        for arguments in (
            {"channel_id": 8, "text": "Synthetic answer"},
            {"channel_id": 2, "text": "@everyone Synthetic answer", "ping_everyone": True},
            {"channel_id": 2, "text": "@here Synthetic answer"},
            {"channel_id": 2, "text": "<@&9> Synthetic answer"},
            {"channel_id": 2, "text": "Synthetic answer", "file_name": "synthetic.txt"},
        ):
            with self.subTest(arguments=arguments):
                self.context.state.proposed_changes.clear()
                other = SimpleNamespace(**{**vars(self.channel), "id": arguments["channel_id"]})
                with patch(
                    "elbow_helper.agent.discord_actions.messages._channel_for_post",
                    new_callable=AsyncMock, return_value=other,
                ), patch(
                    "elbow_helper.agent.discord_actions.messages.resolve_post_attachment",
                    new_callable=AsyncMock, return_value=None,
                ):
                    result = await prepare_post(self.context, arguments)
                self.assertEqual(result["status"], "confirmation_required")
                self.assertTrue(self.context.state.proposed_changes)
                self.context.send_reply.assert_not_awaited()
