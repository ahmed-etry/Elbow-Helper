"""Confirmed posts, edits and DMs send images without captions."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from features.agent.discord_actions import test_agent_direct_messages as dm_fixtures
from features.agent.discord_actions import test_agent_discord_messages as fixtures
from elbow_helper.features.agent.discord_actions.direct_messages import deliver_dms
from elbow_helper.features.agent.discord_actions.message_parts import outgoing_parts
from elbow_helper.features.agent.discord_actions.messages import (
    prepare_edit, prepare_edit_undo, prepare_post,
)


GIF = "https://giphy.com/gifs/synthetic-example"


class OutgoingGifTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.DiscordMessageActionTests.asyncSetUp(self)
        self.context.state.request_text = "Reply to <@7> with a GIF."
        self.context.source_message.raw_mentions = [7]
        self.guard = patch(
            "elbow_helper.features.agent.discord_actions.messages.require_evidence_access",
            new_callable=AsyncMock,
        )
        self.guard.start()
        self.addCleanup(self.guard.stop)

    async def test_mixed_post_preview_and_delivery_contain_only_the_image(self):
        await prepare_post(self.context, {"channel_id": 2, "text": "Hello <@7>\n" + GIF})
        self.assertEqual(len(self.context.state.proposed_changes), 1)
        action = self.context.state.proposed_changes.pop()
        outcome = await action.run()
        self.assertTrue(await action.verify())
        gif, = self.channel.send.await_args_list
        self.assertEqual(gif.args, (GIF,))
        self.assertEqual(gif.kwargs["allowed_mentions"].to_dict()["parse"], [])
        self.assertIsInstance(outcome.result["message_id"], int)

    async def test_image_only_reply_keeps_the_reply_target(self):
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
            self.channel.send.reset_mock()
            await prepare_post(self.context, {
                "channel_id": 2, "text": text, "reply_to": 55, "ping_author": True,
            })
            await self.context.state.proposed_changes.pop().run()
            first = self.channel.send.await_args_list[0]
            self.assertEqual(first.kwargs["reference"].message_id, 55)
            last = self.channel.send.await_args_list[-1]
            self.assertEqual(last.args, (GIF,))
            self.assertFalse(last.kwargs["allowed_mentions"].replied_user)
            self.assertEqual(self.channel.send.await_count, 1)

    async def test_edit_uses_only_gif_and_undo_restores_previous_text(self):
        await prepare_post(self.context, {"channel_id": 2, "text": "Before"})
        posted = await self.context.state.proposed_changes.pop().run()
        identity = posted.result["message_id"]
        await prepare_edit(self.context, {
            "channel_id": 2, "message_id": identity, "text": "After\n" + GIF,
        })
        action = self.context.state.proposed_changes.pop()
        result = await action.run()
        self.assertTrue(await action.verify())
        self.assertEqual(self.messages[identity].content, GIF)
        self.assertEqual(result.after["gif_messages"], {})
        undo = await prepare_edit_undo(self.context, {
            "targets": action.values, "before": action.preview.before, "after": result.after,
        })
        self.assertTrue(await undo.preview.recheck())
        await undo.run()
        self.assertTrue(await undo.verify())
        self.assertEqual(self.messages[identity].content, "Before")

    async def test_image_dm_omits_caption_and_text_attribution(self):
        dm_fixtures.DirectMessageTests.setUp(self)
        with patch(
            "elbow_helper.features.agent.discord_actions.direct_messages.has_access_requirements",
            return_value=True,
        ):
            result = await deliver_dms(self.context, [2], "Hello <@7>\n" + GIF)
        self.assertTrue(result[0]["delivered"])
        gif, = self.members[2].send.await_args_list
        self.assertEqual(gif.args, (GIF,))
        self.assertEqual(gif.kwargs["allowed_mentions"].to_dict()["parse"], [])

    async def test_gif_only_dm_has_no_text_or_pings(self):
        dm_fixtures.DirectMessageTests.setUp(self)
        result = await deliver_dms(self.context, [1], GIF)
        self.assertTrue(result[0]["delivered"])
        self.members[1].send.assert_awaited_once()
        self.assertEqual(self.members[1].send.await_args.args, (GIF,))
        self.assertEqual(
            self.members[1].send.await_args.kwargs["allowed_mentions"].to_dict()["parse"], [],
        )

    async def test_failed_gif_send_does_not_verify_as_a_complete_post(self):
        await prepare_post(self.context, {"channel_id": 2, "text": "Answer\n" + GIF})
        action = self.context.state.proposed_changes.pop()
        original = self.channel.send.side_effect

        async def send(text, **options):
            if text == GIF:
                raise OSError("Synthetic delivery failure")
            return await original(text, **options)

        self.channel.send.side_effect = send
        with self.assertRaises(OSError):
            await action.run()
        self.assertFalse(await action.verify())
        self.assertEqual(self.channel.send.await_count, 1)

    def test_provider_links_discard_captions_and_markdown_labels(self):
        for host in ("giphy.com", "tenor.com", "klipy.com"):
            link = f"https://{host}/synthetic"
            self.assertEqual(outgoing_parts(link), ((link, True),))
            self.assertEqual(outgoing_parts("Text <" + link + ">"),
                             ((link, True),))
            self.assertEqual(outgoing_parts("[Text](" + link + ")"),
                             ((link, True),))
        self.assertEqual(outgoing_parts("https://example.com/page"),
                         (("https://example.com/page", False),))
