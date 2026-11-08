"""Confirmed posts, edits and DMs send the requested text whole, GIF links included."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


from agent.discord_actions import test_agent_direct_messages as dm_fixtures
from agent.discord_actions import test_agent_discord_messages as fixtures
from elbow_helper.agent.discord_actions.direct_messages import (
    deliver_dms, prepare_direct_messages,
)
from elbow_helper.agent.discord_actions.messages import (
    prepare_edit, prepare_edit_undo, prepare_post,
)


GIF = "https://tenor.com/view/synthetic-example"
TEXT = "Big win this week! <@7>\n" + GIF
DM_TEXT = "Big win this week! <@2>\n" + GIF
IMAGE = SimpleNamespace(filename="synthetic-chart.png", data=b"synthetic image")


class OutgoingGifTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.DiscordMessageActionTests.asyncSetUp(self)
        self.context.state.request_text = "Reply to <@7> with a GIF."
        self.context.source_message.raw_mentions = [7]
        self.guard = patch(
            "elbow_helper.agent.discord_actions.messages.require_evidence_access",
            new_callable=AsyncMock,
        )
        self.guard.start()
        self.addCleanup(self.guard.stop)

    async def test_post_sends_the_full_text_once_as_previewed(self):
        await prepare_post(self.context, {"channel_id": 2, "text": TEXT})
        action, = self.context.state.proposed_changes
        self.assertEqual(action.preview.details, tuple(TEXT.splitlines()))
        self.assertIn("Pings: Example (@example.7)", action.preview.lines)
        await action.run()
        self.assertTrue(await action.verify())
        sent, = self.channel.send.await_args_list
        self.assertEqual(sent.args, (TEXT,))
        self.assertEqual("\n".join(action.preview.details), sent.args[0])
        self.assertTrue(sent.kwargs["allowed_mentions"].users)

    async def test_reply_with_gif_link_keeps_text_reply_target_and_ping(self):
        self.context.guild.get_member = lambda _: self.context.member
        self.channel.permissions_for = lambda _: SimpleNamespace(
            view_channel=True, send_messages=True, read_message_history=True,
        )
        self.messages[55] = SimpleNamespace(
            id=55, channel=self.channel,
            author=SimpleNamespace(id=7, display_name="Example", name="example.7"),
            content="Question", jump_url="https://discord.com/channels/3/2/55",
        )
        for text in ("Answer\n" + GIF, GIF):
            with self.subTest(text=text):
                self.channel.send.reset_mock()
                await prepare_post(self.context, {
                    "channel_id": 2, "text": text, "reply_to": 55, "ping_author": True,
                })
                action, = self.context.state.proposed_changes
                self.context.state.proposed_changes.clear()
                self.assertTrue(any(line.startswith("Pings:") for line in action.preview.lines))
                await action.run()
                sent, = self.channel.send.await_args_list
                self.assertEqual(sent.args, (text,))
                self.assertEqual(sent.kwargs["reference"].message_id, 55)
                self.assertTrue(sent.kwargs["allowed_mentions"].replied_user)


    async def test_image_file_post_keeps_sending_the_image_without_a_caption(self):
        self.context.state.attachments = [IMAGE]
        await prepare_post(self.context, {
            "channel_id": 2, "text": TEXT, "file_name": IMAGE.filename,
        })
        action, = self.context.state.proposed_changes
        self.assertFalse(any(line.startswith("Pings:") for line in action.preview.lines))
        await action.run()
        sent, = self.channel.send.await_args_list
        self.assertEqual(sent.args, ("",))
        self.assertEqual(sent.kwargs["file"].filename, IMAGE.filename)
        self.assertEqual(sent.kwargs["allowed_mentions"].to_dict()["parse"], [])

    async def test_edit_keeps_the_full_text_in_one_message_and_undo_restores_it(self):
        await prepare_post(self.context, {"channel_id": 2, "text": "Before"})
        posted = await self.context.state.proposed_changes.pop().run()
        identity = posted.result["message_id"]
        self.channel.send.reset_mock()
        await prepare_edit(self.context, {
            "channel_id": 2, "message_id": identity, "text": TEXT,
        })
        action = self.context.state.proposed_changes.pop()
        self.assertEqual(action.preview.details, (TEXT,))
        self.assertIn("Pings: Example (@example.7)", action.preview.lines)
        result = await action.run()
        self.assertTrue(await action.verify())
        self.assertEqual(self.messages[identity].content, TEXT)
        self.assertEqual(self.messages[identity].content, action.preview.details[0])
        self.channel.send.assert_not_awaited()
        self.assertEqual(result.after, {"content": TEXT})
        undo = await prepare_edit_undo(self.context, {
            "targets": action.values, "before": action.preview.before, "after": result.after,
        })
        self.assertTrue(await undo.preview.recheck())
        await undo.run()
        self.assertTrue(await undo.verify())
        self.assertEqual(self.messages[identity].content, "Before")


    async def test_dm_sends_the_full_text_once_as_previewed(self):
        dm_fixtures.DirectMessageTests.setUp(self)
        with (
            patch("elbow_helper.agent.discord_actions.direct_messages.require_evidence_access",
                  AsyncMock()),
            patch("elbow_helper.agent.discord_actions.direct_messages.has_access_requirements",
                  return_value=True),
            patch("elbow_helper.agent.discord_actions.direct_messages.resolve_post_attachment",
                  AsyncMock(return_value=None)),
        ):
            await prepare_direct_messages(self.context, {"member_ids": [1], "text": DM_TEXT})
            action, = self.context.state.proposed_changes
            await action.run()
        self.assertIn("Pings: Synthetic 2", action.preview.lines)
        sent, = self.members[1].send.await_args_list
        self.assertEqual(sent.args, (DM_TEXT,))
        self.assertEqual("\n".join(action.preview.details), sent.args[0])
        self.assertTrue(sent.kwargs["allowed_mentions"].users)

    async def test_dm_for_someone_else_keeps_text_and_attribution(self):
        dm_fixtures.DirectMessageTests.setUp(self)
        with patch(
            "elbow_helper.agent.discord_actions.direct_messages.has_access_requirements",
            return_value=True,
        ):
            result = await deliver_dms(self.context, [2], DM_TEXT)
        self.assertTrue(result[0]["delivered"])
        sent, = self.members[2].send.await_args_list
        self.assertEqual(sent.args, (DM_TEXT + "\n\nSynthetic 1 asked me to send this.",))


    async def test_image_file_dm_keeps_sending_the_image_without_a_caption(self):
        dm_fixtures.DirectMessageTests.setUp(self)
        with patch(
            "elbow_helper.agent.discord_actions.direct_messages.has_access_requirements",
            return_value=True,
        ):
            await deliver_dms(self.context, [2], DM_TEXT, IMAGE)
        sent, = self.members[2].send.await_args_list
        self.assertEqual(sent.args, ("",))
        self.assertEqual(sent.kwargs["file"].filename, IMAGE.filename)
        self.assertEqual(sent.kwargs["allowed_mentions"].to_dict()["parse"], [])
