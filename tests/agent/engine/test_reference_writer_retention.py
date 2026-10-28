"""Selected reference facts survive paging, with normal authorization checks."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.engine.cohort_evidence import cohort_writer_evidence
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.member_facts import reference_facts
from elbow_helper.agent.families.core import family_tool
from elbow_helper.agent.plan.results import compact_result
from agent.engine.test_agent_plan_flow import _context, _plan, _Session
from agent.engine.test_complete_read_composition import CaptureModel
from agent.engine.test_writer_tables import expand


class ReferenceWriterRetentionTests(unittest.IsolatedAsyncioTestCase):
    def test_compact_role_and_workflow_references_keep_the_same_semantic_facts(self):
        scores = [{'workflow': 'ass_scoring', 'score': i} for i in range(30)]
        role_rows = [{'subject': f'Role {i}', 'holds': []} for i in range(30)]
        payloads = [
            {'mode': 'implementation_reference', 'rows': scores},
            {'reference_kind': 'roles', 'rows': role_rows},
            {'rows': [{'body': 'Selected policy.'}],
             'implementation_reference': {'mode': 'implementation_reference', 'rows': scores}},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertEqual(reference_facts(compact_result(payload), _context().guild),
                                 reference_facts(payload, _context().guild))

    async def make_flow(self, mode, rows):
        source = AsyncMock(return_value={'rows': rows, 'mode': mode})
        tool = family_tool('read_community_reference', 'Read selected facts.', {}, source)
        model = CaptureModel(_Session([], []))
        with patch('elbow_helper.agent.engine.service.build_agent_tools',
                   return_value={'read_community_reference': tool}):
            flow, _ = AgentService(model)._start_flow(question='Summarize the selected reference facts',
                local_context='', context=_context(), conversation_history='')
        flow.plan = {**_plan([{'id': 'facts', 'capability': 'read_community_reference',
            'arguments': {'limit': 1}, 'depends_on': []}]), 'finish_after_reads': True}
        results = await flow.runner.run(flow.plan)
        flow.completed_results.update(results)
        flow.result_views.update(flow.runner.model_results_for(results))
        return flow, model, source

    async def test_full_selected_reference_reaches_writer_beyond_transport_character_room(self):
        rows = [{'identity': i, 'body': ('Selected reference content. ' * 100) + str(i)}
                for i in range(30)]
        for mode in ('sections', 'domain_facts', 'implementation_reference'):
            with self.subTest(mode=mode):
                selected = ([dict(row, workflow='ass_scoring') for row in rows]
                            if mode == 'implementation_reference' else rows)
                flow, model, source = await self.make_flow(mode, selected)
                self.assertEqual(len(flow.completed_results['facts']['rows']), 1)
                self.assertTrue(await flow._prepare_answer_session())
                visible = expand(json.loads(model.writer['prompt'].split('\nAvailable evidence:\n')[1]))
                self.assertEqual(visible['results']['facts']['rows'], rows)
                self.assertTrue(flow._planned_reads_complete(flow.completed_results))
                source.assert_awaited_once()

    async def test_index_is_navigation_and_does_not_expand_into_a_full_reference(self):
        rows = [{'heading': 'Section ' + str(i)} for i in range(30)]
        flow, model, _ = await self.make_flow('index', rows)
        self.assertTrue(await flow._prepare_answer_session())
        visible = expand(json.loads(model.writer['prompt'].split('\nAvailable evidence:\n')[1]))
        self.assertEqual(visible['results']['facts']['rows'], rows[:1])
        self.assertFalse(flow._planned_reads_complete(flow.completed_results))

    async def test_revoked_selected_reference_does_not_restore_its_hidden_rows(self):
        flow, _, _ = await self.make_flow('sections', [{'body': str(i)} for i in range(30)])
        with patch('elbow_helper.agent.engine.cohort_evidence.authorize_report',
                   AsyncMock(return_value=False)):
            visible = await cohort_writer_evidence(flow.context, flow.result_views)
        self.assertEqual(visible['facts']['cause'], 'report_unavailable')
        self.assertNotIn('rows', visible['facts'])
