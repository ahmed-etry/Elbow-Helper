"""SQL summaries fit the checked plan and retain exact cut counts at every boundary."""

import asyncio
from dataclasses import replace
import json
import unittest
from tests.temp_directory import TemporaryDirectory
from unittest.mock import Mock, patch

from tests.agent.engine.test_agent_plan_flow import _context, _Model, _Session, _plan
from tests.agent.engine.test_writer_tables import expand
from tests.agent.reads.dataset_helpers import synthetic_datasets
from elbow_helper.agent.reads.sql.guide import DataGuide
from elbow_helper.agent.reads.sql.tools import dataset_tools
from elbow_helper.agent.engine.answer_inputs import answer_evidence, bounded_answer_evidence
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.tool_call import bound_tool_result
from elbow_helper.agent.plan.results import model_view


def family_rows():
    return [{'player_tag': '#SYN' + str(i), 'name': 'Synthetic account ' + str(i),
             'member_id': 100000000000000000 + i, 'early_n': 20, 'recent_n': 30,
             'early_stars': 2.5, 'recent_stars': 2.1} for i in range(700)]


class SqlResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovered_sql_returns_every_account_through_the_planner_and_writer(self):
        rows = family_rows()

        async def query(context, arguments):
            return dict(source='bot_data', rows=rows, row_count=len(rows), truncated=False)

        tool = replace(dataset_tools()[0], handler=query, default_visible=False, specialist_group='records')
        context = _context()
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        guide = await asyncio.to_thread(lambda: DataGuide(synthetic_datasets(directory.name)))
        session = _Session([], [])
        session.replace_tools = Mock()
        with patch('elbow_helper.agent.engine.service.build_agent_tools', return_value={'query_bot_data': tool}):
            flow, _ = AgentService(_Model(session), data_guide=guide)._start_flow(
                question='Synthetic custom comparison', local_context='', context=context, conversation_history='')
        self.assertNotIn('query_bot_data', flow.registry)
        opened = await flow.runner.run(_plan([dict(id='guide', capability='open_capability_group',
            arguments={'group': 'records', 'intent': 'inspect'}, depends_on=[], reason='Synthetic schema')]))
        guide_view = flow.runner.model_results_for(opened)['guide']
        self.assertGreater(len(guide_view['data_guide']), 16000)
        self.assertIn('family.war_attacks', guide_view['data_guide'])
        self.assertIn('family.cwl_attacks', guide_view['data_guide'])
        self.assertIn('query_bot_data', flow.registry)
        plan = _plan([dict(id='summary', capability='query_bot_data', arguments={'sql': 'SELECT 1'},
                           depends_on=[], reason='Synthetic comparison')])
        results = await flow.runner.run(plan)
        view = flow.runner.model_results_for(results)['summary']
        self.assertGreater(len(json.dumps(view)), 16000)
        self.assertFalse(view['truncated'])
        self.assertEqual(view['row_count'], 700)
        self.assertEqual(len(view['rows']['rows']), 700)
        final = expand(json.loads(bounded_answer_evidence(answer_evidence({'summary': view}), 100000)))
        self.assertEqual(final['results']['summary']['rows'], rows)
        self.assertFalse(final['results']['summary']['truncated'])

    def test_model_cuts_include_sql_rows_cut_and_count_even_after_an_earlier_sql_cut(self):
        for compact in (False, True):
            with self.subTest(compact=compact):
                payload = dict(source='bot_data', rows=family_rows(), row_count=700,
                               total_rows=1200, rows_cut=500, truncated=True)
                content = model_view(payload) if compact else payload
                encoded = bound_tool_result(json.dumps(content), 4000)
                result = json.loads(encoded)
                rows = result['rows']['rows'] if compact else result['rows']
                self.assertLessEqual(len(encoded), 4000)
                self.assertGreater(len(rows), 0)
                self.assertEqual(result['row_count'], len(rows))
                self.assertEqual(result['rows_cut'], 1200-len(rows))
                self.assertEqual(result['total_rows'], 1200)
                self.assertIn(str(result['rows_cut']), result['instruction'])
