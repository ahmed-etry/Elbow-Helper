"""Exercise causal target provenance through the public service entry point."""

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest

from elbow_helper.features.agent.engine.service import AgentService
from features.agent.conversation.test_agent_context import _turn
from features.agent.engine.helpers import social_step
from features.agent.engine.test_agent_plan_flow import _context
from features.agent.engine.test_agent_service import _AgentModel, _AgentSession


class ReplyProvenanceRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_answer_restores_causal_subject_after_clearing_previous_request_targets(self):
        context = _context()
        subject = SimpleNamespace(id=42, display_name='Current subject name')
        members = {41: context.member, 42: subject, 999: context.guild.me}
        context.guild.get_member = members.get
        context.source_message.reference = SimpleNamespace(message_id=1001, channel_id=91)
        replied = SimpleNamespace(id=1001, author=SimpleNamespace(id=999, display_name='Bot'),
                                  content='Earlier answer about the subject.', reference=None)
        context = replace(context, attachment_sources=(replied,))
        turn = _turn(1, channel=91)
        turn = replace(turn, record=replace(turn.record, member_id=41, report_ids=()),
                       text=json.dumps({'resolved_entities': [
                           {'kind': 'member', 'id': 42, 'display_name': 'Old subject name'}]}))
        context.state.authorized_history = (turn,)
        context.state.resolved_members[77] = 'Unrelated previous request target'
        context.state.member_self_target = True
        expected = '<@42> keeps the cardboard crown.'
        session = _AgentSession([social_step(expected, meme_id=None) for _ in range(3)])
        answer = await AgentService(_AgentModel(session, route='social')).answer(
            question='Give him another line.', local_context='', context=context)
        self.assertEqual(answer, expected)
        self.assertEqual(context.state.resolved_members, {42: 'Current subject name'})
        self.assertFalse(context.state.member_self_target)
        self.assertEqual(context.state.reply_context['author']['member_id'], 999)
        self.assertEqual(context.state.reply_context['conversation']['requester_id'], 41)
        self.assertEqual(len(session.calls), 1)
