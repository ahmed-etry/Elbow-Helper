"""Synthetic replays retain exact delivered text and current source access."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from elbow_helper.agent.safety.access import AgentAccessLost
from elbow_helper.agent.conversation.replay import replay_answer
from elbow_helper.agent.conversation.state import ConversationRecord, ConversationTurn
from elbow_helper.agent.conversation.wording import ACTION_EMPTY, ACTION_PRIVATE_ANSWER
from tests.agent.engine.test_agent_plan_flow import _context


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
                answer = await replay_answer("replay", context)
                self.assertEqual(answer, "Exact answer.\nSecond line.")
                model.create_agent_session.assert_not_called()

    async def test_legacy_private_notice_never_replays_hidden_generated_text(self):
        from datetime import datetime
        for created in ("2026-01-01T00:05:00+00:00", "2026-01-01T00:11:15+00:00"):
            retained = turn(delivered=ACTION_PRIVATE_ANSWER, generated="Retained private answer.")
            context = replace(_context(), history=(retained,))
            context.source_message.created_at = datetime.fromisoformat(created)
            self.assertEqual(await replay_answer("replay", context), ACTION_EMPTY)
            self.assertEqual(context.state.source_channels, {91})

    async def test_partial_delivery_replays_acknowledged_text_only(self):
        context = replace(_context(), history=(turn(delivered="First part", generated="Full answer", complete=False),))
        self.assertEqual(await replay_answer("replay", context), "First part")

    async def test_reference_selects_exact_turn_and_missing_target_never_falls_back(self):
        context = replace(_context(), history=(turn(), replace(turn(delivered="New answer"), record=replace(
            turn().record, request_message_id=60, reply_ids=(61,), delivered_answer="New answer"))))
        context.source_message.reference = SimpleNamespace(message_id=51)
        self.assertEqual(await replay_answer("replay", context), "Exact answer.\nSecond line.")
        context.source_message.reference.message_id = 999
        self.assertEqual(await replay_answer("replay", context), ACTION_EMPTY)


    async def test_evicted_latest_record_does_not_replay_older_answer(self):
        unavailable = ConversationTurn("{}", frozenset({91}), retention_limited=True)
        context = replace(_context(), history=(turn(), unavailable))
        self.assertEqual(await replay_answer("replay", context), ACTION_EMPTY)

    async def test_new_requester_does_not_inherit_original_requester_access(self):
        context = replace(_context(), history=(turn(access=frozenset({"lead"})),))
        context.member.id = 99
        with self.assertRaises(AgentAccessLost):
            await replay_answer("replay", context)


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
                    await replay_answer("replay", context)
