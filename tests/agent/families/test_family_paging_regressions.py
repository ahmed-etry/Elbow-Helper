"""Both family paging boundaries preserve complete records and live cursors."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from elbow_helper.agent.engine.budgets import ContextBudget
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.steps import PlanRunner
from elbow_helper.agent.engine.tool_call import bound_tool_result
from elbow_helper.agent.families.core import family_tool
from elbow_helper.agent.plan.results import model_result, model_view
from agent.engine.test_agent_plan_flow import _context


def decode_rows(value):
    return [dict(zip(value['columns'], row)) for row in value['rows']] if isinstance(value, dict) else value


class FamilyPagingRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_reuses_summary_with_shrinking_page_budgets_without_gaps(self):
        expected = [{'member_id': index, 'value': 'x' * 180} for index in range(60)]

        async def source(context, arguments):
            return {'rows': expected, 'source_summary': 'stable'}

        tool = family_tool('read_test_family', 'Read records.', {}, source)
        context = _context()
        runner = PlanRunner(service=AgentService(None), context=context,
            registry={tool.definition.name: tool}, command_capabilities={},
            budget=ContextBudget(None, 8192, 0), rounder=SimpleNamespace(social_route=False))
        observed, arguments = [], {'limit': 12}
        while True:
            step_id = 'page' + str(len(observed))
            step = {'id': step_id, 'capability': tool.definition.name,
                    'arguments': arguments, 'depends_on': []}
            with patch.object(ContextBudget, 'result_character_limit',
                              return_value=5000 if len(observed) < 24 else 2300):
                results = await runner.run({'steps': [step]})
            self.assertNotIn('error', results[step_id])
            page = runner.model_results_for(results)[step_id]
            self.assertEqual(page['kind'], 'family')
            self.assertEqual(page['requester_id'], context.member.id)
            self.assertFalse(page['flags']['truncated'])
            rows = decode_rows(page['rows'])
            self.assertTrue(rows)
            self.assertEqual(page['returned_rows'], len(rows))
            observed.extend(rows)
            if page['next_cursor'] is None:
                break
            report = context.state.reports[page['report_id']]
            self.assertEqual(report.cursor_offset(page['next_cursor']), len(observed))
            arguments = {'report_id': page['report_id'], 'cursor': page['next_cursor'], 'limit': 12}
        self.assertEqual(observed, expected)
        self.assertTrue(runner.sent_summaries)

    async def test_each_boundary_returns_a_resumable_complete_page(self):
        expected = [{'member_id': index, 'value': 'x' * 350} for index in range(80)]

        async def source(context, arguments):
            return {'rows': expected}

        tool = family_tool('read_test_family', 'Read synthetic records.', {}, source)
        context = _context()
        result = await tool.handler(context, {})
        self.assertLess(len(result['rows']), len(expected))
        observed = []
        while True:
            view = json.dumps(model_view(model_result(result)), ensure_ascii=False, separators=(',', ':'))
            bounded = json.loads(bound_tool_result(view, 4500))
            self.assertFalse(bounded.get('truncated'))
            rows = decode_rows(bounded['rows'])
            self.assertEqual(bounded['returned_rows'], len(rows))
            observed.extend(rows)
            if bounded['next_cursor'] is None:
                break
            result = await tool.handler(context, {'report_id': result['report_id'], 'cursor': bounded['next_cursor']})
        self.assertEqual(observed, expected)
        self.assertFalse(context.state.attachments)
