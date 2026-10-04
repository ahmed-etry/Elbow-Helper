"""Restricted answers require the asker's deliberate disclosure."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.delivery import AgentDeliveryMixin
from elbow_helper.features.agent.models import AgentAttachment, AgentTurnState, AgentDelivery
from elbow_helper.features.agent.conversation.state import Conversation, ConversationRecord, ConversationTurn
from elbow_helper.features.agent.wording import ACTION_PRIVATE_ANSWER, ACTION_RESULT_EXPIRED, ACTION_RESULT_OWNER


class PrivateAnswerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.member = SimpleNamespace(id=2, bot=False)
        self.sent = SimpleNamespace(id=7, edit=AsyncMock(), delete=AsyncMock())
        self.sent.edit.return_value = self.sent
        self.message = SimpleNamespace(id=5, author=self.member, mentions=(),
            channel=SimpleNamespace(id=10, send=AsyncMock(return_value=SimpleNamespace(id=8))), reply=AsyncMock(return_value=self.sent), archive_reply=False)
        self.context = SimpleNamespace(member=self.member, guild=object(), source_message=self.message,
            disclosure_thread_members={}, state=AgentTurnState(source_channels={10, 20}, required_access={"lead"}))
        self.delivery = AgentDeliveryMixin()
        self.delivery.bot = SimpleNamespace(user=SimpleNamespace(id=99))
        self.delivery._conversations = SimpleNamespace(register_reply=Mock())
        self.file = AgentAttachment("result.csv", b"private report")
        self.conversation = Conversation(1, 10)
        self.conversation.append(ConversationTurn("Synthetic turn", frozenset({10, 20}),
            record=ConversationRecord(5, 2, "2026-01-01T00:00:00+00:00", "Synthetic request",
                "Secret answer", ACTION_PRIVATE_ANSWER, "", (), (), (7,), True)))
        for target in ("delivery", "actions.answer_view"):
            guard = patch(f"elbow_helper.features.agent.{target}.require_evidence_access",
                          new=AsyncMock(return_value={20: object()}))
            guard.start()
            self.addCleanup(guard.stop)

    def interaction(self, identifier=2):
        return SimpleNamespace(id=33, user=SimpleNamespace(id=identifier),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()))

    async def notice(self, response="Secret answer", **kwargs):
        with patch("elbow_helper.features.agent.delivery.can_show", new=AsyncMock(return_value=False)) as audience:
            await self.delivery.send_response(self.message, response, None, [self.file],
                self.conversation, context=self.context, **kwargs)
        audience.assert_awaited_once()
        call = self.message.reply.await_args
        self.assertNotIn("Secret", call.args[0])
        self.assertNotIn("files", call.kwargs)
        return call.kwargs["view"]

    async def test_notice_and_private_result_include_no_public_answer(self):
        view = await self.notice()
        self.assertEqual(self.message.reply.await_args.args[0], ACTION_PRIVATE_ANSWER)
        self.assertEqual(view.timeout, 600)
        interaction = self.interaction()
        await view.open_result(interaction)
        call = interaction.response.send_message.await_args
        self.assertEqual(call.args[0], "Secret answer")
        self.assertTrue(call.kwargs["ephemeral"])
        self.assertEqual(call.kwargs["files"][0].filename, "result.csv")

    async def test_post_here_registers_answer_removes_buttons_and_logs_override(self):
        view = await self.notice()
        with self.assertLogs("elbow_helper.features.agent.actions.answer_view", level="INFO") as logs:
            await view.post_here(self.interaction())
        self.message.reply.assert_awaited_once()
        call = self.sent.edit.await_args
        self.assertEqual(call.kwargs["content"], "Secret answer")
        self.assertIsNone(call.kwargs["view"])
        self.assertEqual(call.kwargs["attachments"][0].filename, "result.csv")
        self.delivery._conversations.register_reply.assert_called_with(self.conversation, 7)
        self.sent.delete.assert_not_awaited()
        record = self.conversation.turns[0].record
        self.assertEqual(record.delivered_answer, "Secret answer")
        self.assertEqual(record.reply_ids, (7,))
        self.assertTrue(record.delivery_complete)
        for value in ("requester=2", "channel=10", "sources=[10, 20]", "levels=['lead']"):
            self.assertIn(value, logs.output[0])

    async def test_first_chunk_replaces_notice_and_followups_are_recorded(self):
        response = "Synthetic answer. " * 200
        view = await self.notice(response=response, delivery=AgentDelivery())
        self.message.archive_reply = True
        self.delivery._archive_reply = AsyncMock()
        self.delivery.persistence = SimpleNamespace(save=AsyncMock())
        await view.post_here(self.interaction())
        first = self.sent.edit.await_args.kwargs["content"]
        later = [call.args[0] for call in self.message.channel.send.await_args_list]
        self.assertTrue(later)
        self.assertTrue(all(len(part) <= 2000 for part in (first, *later)))
        self.assertTrue(all("files" not in call.kwargs for call in self.message.channel.send.await_args_list))
        self.assertEqual(self.conversation.turns[0].record.delivered_answer, "\n".join((first, *later)))
        self.assertEqual(self.conversation.turns[0].record.reply_ids, (7, 8))
        self.assertEqual(self.delivery._archive_reply.await_args_list[0].args, (5, 7, first))
        self.assertEqual(self.delivery._archive_reply.await_args_list[-1].args, (5, 8, later[-1]))
        self.delivery.persistence.save.assert_awaited_once_with(self.delivery._conversations, self.conversation)

    async def test_member_mentions_send_reply_then_delete_notice(self):
        mentioned = SimpleNamespace(id=3)
        self.message.mentions = (mentioned,)
        view = await self.notice(response="Answer for <@3>")
        answer = SimpleNamespace(id=9)
        self.message.reply.return_value = answer
        events = []
        async def reply(*args, **kwargs):
            events.append("answer")
            return answer
        async def delete():
            events.append("delete notice")
        self.message.reply.side_effect = reply
        self.sent.delete.side_effect = delete
        await view.post_here(self.interaction())
        self.assertEqual(events, ["answer", "delete notice"])
        self.assertEqual(self.message.reply.await_args.kwargs["allowed_mentions"].users, [mentioned])
        self.assertEqual(self.message.reply.await_args.kwargs["files"][0].filename, "result.csv")
        self.sent.edit.assert_not_awaited()
        self.assertEqual(self.conversation.turns[0].record.reply_ids, (9,))
        self.delivery._conversations.register_reply.assert_called_with(self.conversation, 9)

    async def test_access_rechecked_immediately_before_notice_edit(self):
        view = await self.notice()
        with patch("elbow_helper.features.agent.delivery.require_evidence_access",
                   new=AsyncMock(side_effect=AgentAccessLost())):
            with self.assertRaises(AgentAccessLost):
                await view.post_here(self.interaction())
        self.sent.edit.assert_not_awaited()
        self.sent.delete.assert_not_awaited()
        self.assertEqual(self.message.reply.await_count, 1)

    async def test_uncertain_followup_after_notice_edit_can_be_reconciled(self):
        from dataclasses import replace
        from elbow_helper.features.agent.delivery import AgentDeliveryUnknown
        from elbow_helper.features.agent.text import chunk_response
        response = "Synthetic answer. " * 200
        self.conversation.turns[0] = replace(self.conversation.turns[0],
            record=replace(self.conversation.turns[0].record, generated_answer=response))
        view = await self.notice(response=response)
        self.message.channel.send.side_effect = OSError("Synthetic uncertain send")
        self.delivery._find_delivery_nonce = AsyncMock(side_effect=[None, SimpleNamespace(id=8)])
        self.delivery._archive_reply = AsyncMock()
        with self.assertRaises(AgentDeliveryUnknown):
            await view.post_here(self.interaction())
        record = self.conversation.turns[0].record
        self.assertTrue(record.delivery_unknown)
        self.assertEqual(len(record.attempted_nonces), 2)
        self.assertEqual(record.reply_ids, (7,))
        self.assertEqual(await self.delivery._reconcile_unknown_deliveries(
            self.conversation, self.message.channel), 1)
        record = self.conversation.turns[0].record
        self.assertTrue(record.delivery_complete)
        self.assertFalse(record.delivery_unknown)
        self.assertEqual(record.reply_ids, (7, 8))
        self.assertEqual(record.delivered_answer, "\n".join(chunk_response(response)))
    async def test_post_here_decision_reuses_only_subsets_in_same_conversation(self):
        view = await self.notice()
        await view.post_here(self.interaction())
        self.message.reply.reset_mock()
        self.context.state.source_channels = {20}
        with patch("elbow_helper.features.agent.delivery.can_show", new=AsyncMock(return_value=False)):
            with self.assertLogs("elbow_helper.features.agent.delivery", level="INFO") as logs:
                await self.delivery.send_response(self.message, "Synthetic follow-up", None,
                    conversation=self.conversation, context=self.context)
        self.assertEqual(self.message.reply.await_args.args[0], "Synthetic follow-up")
        self.assertNotIn("view", self.message.reply.await_args.kwargs)
        self.assertIn("disclosure reused: requester=2 channel=10 sources=[20] levels=['lead']", logs.output[0])
        for sources, levels, requester in (({20, 30}, {"lead"}, 2),
                                           ({20}, {"lead", "core"}, 2),
                                           ({20}, {"lead"}, 3)):
            self.context.state.source_channels = sources
            self.context.state.required_access = levels
            self.context.member = SimpleNamespace(id=requester)
            with patch("elbow_helper.features.agent.delivery.can_show", new=AsyncMock(return_value=False)):
                await self.delivery.send_response(self.message, "Synthetic restricted answer", None,
                    conversation=self.conversation, context=self.context)
            self.assertEqual(self.message.reply.await_args.args[0], ACTION_PRIVATE_ANSWER)
            self.assertIn("view", self.message.reply.await_args.kwargs)
        self.context.member = self.member
        self.context.state.source_channels = {20}
        self.context.state.required_access = {"lead"}
        with patch("elbow_helper.features.agent.delivery.can_show", new=AsyncMock(return_value=False)):
            await self.delivery.send_response(self.message, "Synthetic other conversation", None,
                conversation=Conversation(1, 10), context=self.context)
        self.assertEqual(self.message.reply.await_args.args[0], ACTION_PRIVATE_ANSWER)
        self.assertFalse(self.conversation.can_reuse_answer_disclosure(2, 11, {20}, {"lead"}))

    async def test_remembered_disclosure_still_requires_source_access(self):
        self.conversation.remember_answer_disclosure(2, 10, {10, 20}, {"lead"})
        with patch("elbow_helper.features.agent.delivery.require_evidence_access",
                   new=AsyncMock(side_effect=AgentAccessLost())):
            with self.assertRaises(AgentAccessLost):
                await self.delivery.send_response(self.message, "Synthetic answer", None,
                    conversation=self.conversation, context=self.context)
        self.message.reply.assert_not_awaited()

    async def test_incomplete_post_does_not_remember_disclosure(self):
        view = await self.notice(response="Synthetic long answer. " * 200)
        self.message.channel.send.side_effect = RuntimeError("Synthetic delivery failure")
        with self.assertRaises(RuntimeError):
            await view.post_here(self.interaction())
        self.assertEqual(self.conversation.answer_disclosures, {})

    async def test_other_members_cannot_use_either_button(self):
        view = await self.notice()
        for callback in (view.open_result, view.post_here):
            interaction = self.interaction(3)
            await callback(interaction)
            interaction.response.send_message.assert_awaited_once_with(ACTION_RESULT_OWNER, ephemeral=True)

    async def test_lost_access_expires_both_buttons(self):
        for method in ("open_result", "post_here"):
            view = await self.notice()
            interaction = self.interaction()
            with patch("elbow_helper.features.agent.actions.answer_view.require_evidence_access",
                       new=AsyncMock(side_effect=AgentAccessLost())):
                await getattr(view, method)(interaction)
            self.assertTrue(all(item.disabled for item in view.children))
            interaction.response.send_message.assert_awaited_once_with(ACTION_RESULT_EXPIRED, ephemeral=True)

    async def test_qualified_channel_posts_normally(self):
        with patch("elbow_helper.features.agent.delivery.can_show", new=AsyncMock(return_value=True)) as audience:
            await self.delivery.send_response(self.message, "Answer", None, [self.file], context=self.context)
        audience.assert_awaited_once()
        self.assertEqual(self.message.reply.await_args.args[0], "Answer")
        self.assertNotIn("view", self.message.reply.await_args.kwargs)

    async def test_scheduled_notice_mentions_requester_and_lasts_one_hour(self):
        self.message.scheduled_run = True
        view = await self.notice()
        self.assertEqual(view.timeout, 3600)
        self.assertEqual(self.message.reply.await_args.args[0], "<@2> " + ACTION_PRIVATE_ANSWER)

    async def test_posting_scheduled_private_answer_edits_notice_without_pinging(self):
        self.message.scheduled_run = True
        answer = "Synthetic scheduled answer for <@2>."
        view = await self.notice(response=answer, mention_requester=True)
        self.assertEqual(self.message.reply.await_args.kwargs["allowed_mentions"].users, [self.member])

        await view.post_here(self.interaction())

        self.message.reply.assert_awaited_once()
        self.message.channel.send.assert_not_awaited()
        self.sent.delete.assert_not_awaited()
        self.sent.edit.assert_awaited_once()
        call = self.sent.edit.await_args.kwargs
        self.assertEqual(call["content"], answer)
        self.assertIsNone(call["view"])
        allowed = call["allowed_mentions"]
        self.assertFalse(allowed.users)
        self.assertFalse(allowed.roles)
        self.assertFalse(allowed.everyone)
        self.assertFalse(allowed.replied_user)
        self.assertEqual(self.conversation.turns[0].record.reply_ids, (self.sent.id,))
