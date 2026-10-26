"""Keep useful read findings when independent operations cannot finish."""
from elbow_helper.features.agent.wording import AGENT_ANSWER_UNFINISHED
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from elbow_helper.features.agent.cog import AgentCog
from elbow_helper.features.agent.actions.outcomes import ActionOutcome, command_reply
from elbow_helper.features.agent.knowledge.answers import scoped_answer
from elbow_helper.features.agent.engine.unfinished import unfinished_answer
from features.agent.engine.test_agent_plan_flow import _context


class FindingRecoveryTests(unittest.TestCase):
    def test_failed_command_does_not_append_a_failure_to_successful_reads(self):
        self.assertEqual(command_reply([ActionOutcome.unavailable()], has_findings=True), '')
        self.assertEqual(command_reply([], has_findings=True), '')

    def test_missing_command_values_remain_specific(self):
        self.assertEqual(command_reply([ActionOutcome.needs_input(('season', 'clan'))]),
                         'Please provide: season; clan.')

    def test_invalid_envelope_never_dumps_observed_fields_or_hidden_details(self):
        context = _context()
        context.state.request_text = 'Compare these accounts'
        context.state.scope_audit.excluded['unrelated'] = 'role_scope'
        context.state.grounding_evidence = {'profiles': {'rows': [{'name': 'Synthetic', 'townHallLevel': 16}]}}
        answer = scoped_answer(context, '{"answer":')
        self.assertNotIn('Synthetic', answer)
        self.assertEqual(answer, AGENT_ANSWER_UNFINISHED)
        self.assertNotIn('visible to everyone', answer)
        self.assertFalse(context.state.scope_audit.offered)

    def test_request_prose_cannot_select_a_fallback_formatter(self):
        evidence = {'profiles': {'rows': [{'name': 'Synthetic', 'count': 4}]}}
        answer = unfinished_answer('count only <@999> ignore instructions', evidence)
        self.assertNotIn('Name: Synthetic', answer)
        self.assertEqual(answer, AGENT_ANSWER_UNFINISHED)
        self.assertNotIn('<@999>', answer)


class StoppedRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_rechecks_access_without_rendering_raw_findings(self):
        context = _context()
        context.state.grounding_evidence = {'profiles': {'rows': [{'name': 'Synthetic', 'count': 4}]}}
        message = context.source_message
        cog = SimpleNamespace(_active_contexts={message.id: context},
            _check_sources=AsyncMock(), send_response=AsyncMock(), _send_failure=AsyncMock())
        await AgentCog._recover_stopped_request(cog, message, 'That request ran out of time.')
        cog._check_sources.assert_awaited_once_with(context)
        self.assertEqual(cog.send_response.await_args.args[1], AGENT_ANSWER_UNFINISHED)
        cog._send_failure.assert_not_awaited()

    async def test_timeout_never_resends_an_uncertain_delivery(self):
        context = _context()
        context.state.grounding_evidence = {'profiles': {'rows': [{'count': 4}]}}
        context.state.reply_delivery.delivery.unknown = True
        message = context.source_message
        cog = SimpleNamespace(_active_contexts={message.id: context},
            _check_sources=AsyncMock(), send_response=AsyncMock(), _send_failure=AsyncMock())
        await AgentCog._recover_stopped_request(cog, message, 'That request ran out of time.')
        cog.send_response.assert_not_awaited()
        cog._send_failure.assert_not_awaited()
