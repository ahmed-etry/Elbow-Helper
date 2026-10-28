"""Synthetic native poll posts are confirmed, while result reads retain channel evidence."""

import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.agent.discord_actions.polls import prepare_poll, read_poll, undo_poll
from agent.discord_actions.management_helpers import Entity, fixture


class PollTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for module in ("management", "polls"):
            access = patch(f"elbow_helper.agent.discord_actions.{module}."
                           "require_evidence_access", new_callable=AsyncMock)
            access.start()
            self.addCleanup(access.stop)

    def context(self):
        context, channel, _, _ = fixture()
        message = Entity(id=900, author=context.guild.me, delete=AsyncMock(),
                         jump_url="https://discord.com/channels/1/200/900")
        channel.send = AsyncMock(return_value=message)
        channel.fetch_message = AsyncMock(return_value=message)
        return context, channel, message

    async def test_poll_uses_native_answers_and_buttons_wait_for_confirmation(self):
        context, channel, _ = self.context()
        arguments = {"channel_id": 200, "question": "Synthetic question?",
                     "answers": ["Synthetic A", "Synthetic B"], "duration_hours": 1,
                     "multiple_choice": True}
        await prepare_poll(context, arguments)
        channel.send.assert_not_called()
        action = context.state.proposed_changes[0]
        self.assertIn("Duration: 1 hour", action.preview.lines)
        result = await action.run()
        poll = channel.send.await_args.kwargs["poll"]
        self.assertIsInstance(poll, discord.Poll)
        self.assertEqual([answer.text for answer in poll.answers], arguments["answers"])
        self.assertTrue(poll.multiple)
        undo = await undo_poll(context, {"targets": action.values, "after": result.after})
        await undo.run()
        channel.fetch_message.return_value.delete.assert_awaited_once()

    async def test_results_are_read_without_confirmation_and_keep_provenance(self):
        context, _, message = self.context()
        message.poll = Entity(question=Entity(text="Synthetic question"),
                              answers=[Entity(id=1, text="Synthetic A", vote_count=7)],
                              multiple=True, expires_at=None, is_finalized=lambda: False)
        result = await read_poll(context, {"channel_id": 200, "message_id": 900})
        self.assertEqual(result["answers"], [{"answer_id": 1, "text": "Synthetic A", "votes": 7}])
        self.assertEqual(context.state.source_channels, {200})
        self.assertFalse(context.state.proposed_changes)

    async def test_missing_send_polls_is_refused_before_preview(self):
        context, channel, _ = self.context()
        context.guild.me.guild_permissions = discord.Permissions(
            view_channel=True, send_messages=True,
        )
        with self.assertRaisesRegex(ValueError, "The bot needs Send Polls"):
            await prepare_poll(context, {"channel_id": 200, "question": "Synthetic",
                                         "answers": ["A", "B"], "duration_hours": 1})
        channel.send.assert_not_called()

    async def test_thread_requires_thread_sending_permission(self):
        context, channel, _ = self.context()
        channel.type = discord.ChannelType.public_thread
        arguments = {"channel_id": 200, "question": "Synthetic",
                     "answers": ["A", "B"], "duration_hours": 1}
        context.member.guild_permissions.send_messages_in_threads = False
        with self.assertRaisesRegex(ValueError, "Send Messages In Threads"):
            await prepare_poll(context, arguments)
        context.member.guild_permissions.send_messages_in_threads = True
        context.member.guild_permissions.send_messages = False
        await prepare_poll(context, arguments)
        await context.state.proposed_changes[0].run()
        channel.send.assert_awaited_once()

    async def test_non_message_channel_is_refused_before_preview(self):
        context, channel, _ = self.context()
        channel.send = None
        with self.assertRaisesRegex(ValueError, "The selected poll is invalid"):
            await prepare_poll(context, {"channel_id": 200, "question": "Synthetic",
                                         "answers": ["A", "B"], "duration_hours": 1})
        self.assertFalse(context.state.proposed_changes)
