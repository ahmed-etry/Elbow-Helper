"""Capabilities survive writer isolation; malformed plans retain useful recovery."""
import json
import unittest
from unittest.mock import AsyncMock

from elbow_helper.agent.reads.snapshots import family_tool
from elbow_helper.agent.engine.cohort_evidence import cohort_writer_evidence
from elbow_helper.agent.plan.results import model_result
from elbow_helper.agent.plan.planning import read_request
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage
from tests.agent.engine.test_agent_plan_flow import _context, _Session
from tests.agent.engine.test_writer_delivery_evidence import unpack


class PlanRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_json_retries_a_checked_read_without_inventing_a_scope_refusal(self):
        from elbow_helper.agent.engine.registry import build_agent_tools
        malformed = AgentStep('', (AgentToolCall('bad', 'submit_request_plan',
            '{"goal":"Open guide", "steps":[{"arguments":{}}], BROKEN}'),), AgentUsage())
        corrected = {'goal': 'Read the selected reference', 'effort': 'low', 'output': 'text', 'steps': [
            {'id': 'guide', 'capability': 'read_community_reference',
             'arguments': {'titles': ['Public']}, 'depends_on': []}]}
        session = _Session([malformed, AgentStep('', (AgentToolCall('fixed', 'submit_request_plan',
            json.dumps(corrected)),), AgentUsage())], [])
        decision = await read_request(session, build_agent_tools(), 71)
        self.assertEqual(decision.plan, corrected)
        feedback = json.loads(session.calls[1][0][0].content)
        self.assertIn('valid JSON object', feedback['error'])
        self.assertIn('No reads ran', feedback['error'])
        self.assertEqual(len(session.calls), 2)


class CompleteEvaluationEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_dependent_score_and_war_pages_reach_writer_whole_and_recheck_access(self):
        context = _context()
        cohort = family_tool('read_member_cohort', 'Selected members.', {}, AsyncMock(return_value={
            'basis': 'role_holders', 'rows': [{'member_id': 41, 'account_tags': ['#P0']}]}))
        selected = await cohort.handler(context, {})
        results = {}
        for name in ('read_cwl_results', 'read_war_attendance'):
            rows = [{'player_tag': '#P' + str(i), 'ass_score': i / 10, 'attacks_used': i,
                     'war_ids': ['W1', 'W2']} for i in range(70)]
            tool = family_tool(name, 'Selected account measures.', {'cohort_ref': {'type': 'string'}},
                               AsyncMock(return_value={'rows': rows}))
            page = await tool.handler(context, {'cohort_ref': selected['report_id'], 'limit': 1})
            self.assertEqual(len(page['rows']), 1)
            results[name] = model_result(page)
        written = unpack(await cohort_writer_evidence(context, results))
        for name in results:
            self.assertEqual(len(written[name]['rows']), 70)
            self.assertEqual(written[name]['rows'][-1]['player_tag'], '#P69')
        self.assertEqual(written['read_war_attendance']['rows'][-1]['wars_observed'], 2)
        self.assertNotIn('war_ids', written['read_war_attendance']['rows'][-1])
        context.state.report_sources.clear()
        denied = await cohort_writer_evidence(context, results)
        self.assertTrue(all('rows' not in value for value in denied.values()))
