"""Synthetic replays retain exact delivered text and current source access."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.conversation.replay import replay_answer
from elbow_helper.features.agent.conversation.state import ConversationRecord, ConversationTurn
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.wording import ACTION_EMPTY, ACTION_PRIVATE_ANSWER
from features.agent.engine.test_agent_plan_flow import _context
from features.agent.actions import test_private_answer as private


def turn(*, delivered="Exact answer.\nSecond line.", generated="Different generated answer.",
         sources=frozenset({91}), access=frozenset(), complete=True):
    return ConversationTurn("{}", sources, required_access=access, record=ConversationRecord(
        request_message_id=50, member_id=41, created_at="2026-01-01T00:00:00+00:00",
        question="Synthetic lookup", generated_answer=generated, delivered_answer=delivered,
        local_context="", evidence=(), report_ids=(), reply_ids=(51,), delivery_complete=complete,
    ))


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_replay_uses_delivered_text_without_model_or_tools(self):
        for question in ("resend", "Post that again!", "please send it again", "repeat your answer"):
            with self.subTest(question=question):
                context = replace(_context(), history=(turn(),))
                model = Mock()
                answer = await AgentService(model).answer(question=question, local_context="", context=context)
                self.assertEqual(answer, "Exact answer.\nSecond line.")
                model.create_agent_session.assert_not_called()

    async def test_private_notice_replays_generated_result_before_and_after_control_expiry(self):
        from datetime import datetime
        for created in ("2026-01-01T00:05:00+00:00", "2026-01-01T00:11:15+00:00"):
            retained = turn(delivered=ACTION_PRIVATE_ANSWER, generated="Retained private answer.")
            context = replace(_context(), history=(retained,))
            context.source_message.created_at = datetime.fromisoformat(created)
            self.assertEqual(await replay_answer("resend", context), "Retained private answer.")
            self.assertEqual(context.state.source_channels, {91})

    async def test_partial_delivery_replays_acknowledged_text_only(self):
        context = replace(_context(), history=(turn(delivered="First part", generated="Full answer", complete=False),))
        self.assertEqual(await replay_answer("resend", context), "First part")

    async def test_reference_selects_exact_turn_and_missing_target_never_falls_back(self):
        context = replace(_context(), history=(turn(), replace(turn(delivered="New answer"), record=replace(
            turn().record, request_message_id=60, reply_ids=(61,), delivered_answer="New answer"))))
        context.source_message.reference = SimpleNamespace(message_id=51)
        self.assertEqual(await replay_answer("resend", context), "Exact answer.\nSecond line.")
        context.source_message.reference.message_id = 999
        self.assertEqual(await replay_answer("resend", context), ACTION_EMPTY)

    async def test_no_retained_result_does_not_create_model_session(self):
        model = Mock()
        answer = await AgentService(model).answer(question="resend", local_context="", context=_context())
        self.assertEqual(answer, ACTION_EMPTY)
        model.create_agent_session.assert_not_called()

    async def test_evicted_latest_record_does_not_replay_older_answer(self):
        unavailable = ConversationTurn("{}", frozenset({91}), retention_limited=True)
        context = replace(_context(), history=(turn(), unavailable))
        self.assertEqual(await replay_answer("resend", context), ACTION_EMPTY)

    async def test_new_requester_does_not_inherit_original_requester_access(self):
        context = replace(_context(), history=(turn(access=frozenset({"lead"})),))
        context.member.id = 99
        with self.assertRaises(AgentAccessLost):
            await replay_answer("resend", context)

    async def test_latest_inaccessible_turn_does_not_replay_older_visible_answer(self):
        context = replace(_context(), history=(turn(), turn(sources=frozenset({999}))))
        # The channel exists but its current permissions deny the requester.
        channel = SimpleNamespace(guild=context.guild, permissions_for=lambda _: SimpleNamespace(
            view_channel=False, read_message_history=False))
        context.guild.get_channel_or_thread = lambda value: channel
        with self.assertRaises(AgentAccessLost):
            await replay_answer("post that again", context)

    async def test_new_instructions_are_not_treated_as_replays(self):
        for question in ("resend the recruitment announcement to everyone", "post that again but change it",
                         "do not resend", "count only", "what does resend mean?"):
            self.assertIsNone(await replay_answer(question, _context()))

    async def test_lost_rollout_access_and_bot_source_access_block_replay(self):
        for loss in ("rollout", "bot_source"):
            with self.subTest(loss=loss):
                context = replace(_context(), history=(turn(),))
                if loss == "rollout":
                    context.member.roles.clear()
                else:
                    context.guild.me = SimpleNamespace(id=999)
                    context.source_message.channel.permissions_for = lambda actor: SimpleNamespace(
                        view_channel=actor.id != 999, read_message_history=True)
                with self.assertRaises(AgentAccessLost):
                    await replay_answer("resend", context)


class ReplayDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        private.PrivateAnswerTests.setUp(self)

    async def test_new_requester_does_not_reuse_old_disclosure(self):
        self.conversation.remember_answer_disclosure(2, 10, {10, 20}, {"lead"})
        self.member.id = 3
        context = replace(_context(), member=self.member, source_message=self.message,
                          state=self.context.state, history=tuple(self.conversation.turns))
        with patch("elbow_helper.features.agent.conversation.replay.require_evidence_access",
                   new=AsyncMock()) as access:
            answer = await replay_answer("resend", context)
        self.assertEqual(answer, "Secret answer")
        self.assertEqual(access.await_count, 2)
        with patch("elbow_helper.features.agent.delivery.can_show", new=AsyncMock(return_value=False)) as audience:
            await self.delivery.send_response(self.message, answer, None,
                                             conversation=self.conversation, context=context)
        audience.assert_awaited_once()
        self.assertEqual(self.message.reply.await_args.args[0], ACTION_PRIVATE_ANSWER)
