"""Confirmed posts, edits and DMs send the requested text whole, GIF links included."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from agent.discord_actions import test_agent_direct_messages as dm_fixtures
from agent.discord_actions import test_agent_discord_messages as fixtures
from agent.discord_actions import test_current_channel_posts as reply_fixtures
from elbow_helper.agent.discord_actions.direct_messages import (
    deliver_dms, prepare_direct_messages,
)
from elbow_helper.agent.discord_actions.message_parts import media_links, outgoing_parts
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

    async def test_failed_post_does_not_verify(self):
        await prepare_post(self.context, {"channel_id": 2, "text": TEXT})
        action = self.context.state.proposed_changes.pop()
        self.channel.send.side_effect = OSError("Synthetic delivery failure")
        with self.assertRaises(OSError):
            await action.run()
        self.assertFalse(await action.verify())
        self.assertEqual(self.messages, {})

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

    async def test_undo_of_an_older_split_edit_removes_its_separate_gif(self):
        await prepare_post(self.context, {"channel_id": 2, "text": "Before"})
        identity = (await self.context.state.proposed_changes.pop().run()).result["message_id"]
        await prepare_post(self.context, {"channel_id": 2, "text": GIF})
        companion = (await self.context.state.proposed_changes.pop().run()).result["message_id"]
        self.messages[identity].content = "After"
        undo = await prepare_edit_undo(self.context, {
            "targets": {"channel_id": 2, "message_id": identity, "text": "After"},
            "before": {"content": "Before"},
            "after": {"content": "After", "gif_messages": {str(companion): GIF}},
        })
        await undo.run()
        self.assertTrue(await undo.verify())
        self.assertEqual(self.messages[identity].content, "Before")
        self.assertNotIn(companion, self.messages)

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

    async def test_failed_dm_is_reported_as_not_delivered(self):
        dm_fixtures.DirectMessageTests.setUp(self)
        self.members[1].send.side_effect = discord.HTTPException(
            SimpleNamespace(status=403, reason="Forbidden"), {"code": 50007, "message": "Synthetic"},
        )
        result = await deliver_dms(self.context, [1], DM_TEXT)
        self.assertFalse(result[0]["delivered"])
        self.members[1].send.assert_awaited_once()

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

    def test_outgoing_parts_keep_links_inside_the_text(self):
        for host in ("giphy.com", "tenor.com", "klipy.com"):
            link = f"https://{host}/synthetic"
            for text in (link, "Text <" + link + ">", "[Text](" + link + ")"):
                self.assertEqual(outgoing_parts(text), (text,))
                self.assertEqual(media_links(text), (link,))
        self.assertEqual(outgoing_parts(" "), ())
        self.assertEqual(outgoing_parts(TEXT, attachment=IMAGE), ("",))
        self.assertEqual(media_links("https://example.com/page"), ())


class CurrentChannelGifTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await reply_fixtures.CurrentChannelPostTests.asyncSetUp(self)

    async def test_current_channel_post_hands_over_the_full_text_and_uses_the_chosen_gif(self):
        self.context.state.gif_url = GIF
        text = "Big win this week!\n" + GIF
        result = await prepare_post(self.context, {"channel_id": 2, "text": text})
        self.assertTrue(result["delivered_as_reply"])
        self.context.send_reply.assert_awaited_once_with(self.context, text)
        self.assertIsNone(self.context.state.gif_url)
        self.assertIn(GIF, self.context.state.used_gif_urls)
