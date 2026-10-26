"""Capabilities survive writer isolation; malformed plans retain useful recovery."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from elbow_helper.features.agent.conversation.context import bot_read_overview
from elbow_helper.features.agent.engine.member_facts import read_topics
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.families import TOOLS
from elbow_helper.features.agent.families.core import family_tool
from elbow_helper.features.agent.engine.cohort_evidence import cohort_writer_evidence, complete_activity_reads
from elbow_helper.features.agent.plan.results import model_result
from elbow_helper.features.agent.families.catalogue import FAMILY_INSTRUCTION
from elbow_helper.features.agent.plan.format import WRITER_INSTRUCTION
from elbow_helper.features.agent.plan.planning import read_request
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage
from features.agent.engine.test_agent_plan_flow import _context, _Session
from features.agent.engine.test_planner_fix2_evidence import WriterModel, unpack


class ReadAwarenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_planner_descriptions_become_member_topics_for_isolated_writer(self):
        context, model = _context(), WriterModel()
        overview = bot_read_overview(context)
        expected = {t.definition.name: t.definition.description for t in TOOLS}
        self.assertEqual({r['capability']: r['summary'] for r in overview['reads']}, expected)
        flow, _ = AgentService(model)._start_flow(question='Describe available member evaluation data.',
            local_context='', context=context, conversation_history='')
        self.assertIn('<bot_read_overview>', model.request['prompt'])
        self.assertEqual(json.loads(flow.answer_context)['bot_read_overview'], read_topics(overview))
        self.assertTrue(await flow._prepare_answer_session())
        header = json.loads(model.writer['prompt'].split('\nAvailable evidence:\n', 1)[0])
        self.assertEqual(json.loads(header['context'])['bot_read_overview'], read_topics(overview))
        self.assertIn('beyond slash commands', model.writer['system_prompt'])
        self.assertEqual(model.session.calls, [])
        self.assertFalse(context.state.proposed_changes)

    def test_cohort_evaluation_and_third_party_questions_have_evidence_guidance(self):
        for clause in ('cohort_ref', 'include_activity=true', 'read_cwl_results',
                       'read_war_attendance', 'Competitive role', 'empty interjection'):
            self.assertIn(clause, FAMILY_INSTRUCTION)
        self.assertIn('add only a useful verified contribution', FAMILY_INSTRUCTION)
        self.assertIn('contribute a verified useful fact', WRITER_INSTRUCTION)


class PlanRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_json_retries_a_checked_read_without_inventing_a_scope_refusal(self):
        from elbow_helper.features.agent.engine.registry import build_agent_tools
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

    async def test_activity_continuation_does_not_evict_selected_scores_to_fill_chat_gaps(self):
        from elbow_helper.features.agent.families.core import FamilyReport
        context = _context()
        report = FamilyReport('cohort', context.guild.id, 'read_member_cohort', json.dumps({
            'rows': [], 'summary': {'basis': 'role_holders', 'padding': 'x' * 400_000,
                'activity_coverage': {'chat_coverage': {'source_reads_complete': False}}}}))
        context.state.reports.update(cohort=report, scores=SimpleNamespace(retained_bytes=600_000))
        runner = SimpleNamespace(tool_calls=1, run=AsyncMock())
        flow = SimpleNamespace(context=context, runner=runner, completed_results={'members': {
            'family': 'read_member_cohort', 'basis': 'role_holders', 'report_id': 'cohort',
            'activity_coverage': {'chat_coverage': {'source_reads_complete': False}}}})
        await complete_activity_reads(flow)
        runner.run.assert_not_awaited()
        self.assertIn('scores', context.state.reports)
