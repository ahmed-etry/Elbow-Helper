"""Both family paging boundaries preserve complete records and live cursors."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from elbow_helper.agent.engine.budgets import ContextBudget
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.steps import PlanRunner
from elbow_helper.agent.engine.tool_call import bound_tool_result
from elbow_helper.agent.tool_support.core import family_tool
from elbow_helper.agent.plan.results import model_result, model_view
from agent.engine.test_agent_plan_flow import _context


def decode_rows(value):
    return [dict(zip(value['columns'], row)) for row in value['rows']] if isinstance(value, dict) else value


class FamilyPagingRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_small_cursor_pages_continue_past_sixteen_dispatches_with_budget_enforced(self):
        expected = [{'member_id': index} for index in range(21)]
        async def source(context, arguments):
            return {'rows': expected}
        tool = family_tool('read_test_family', 'Read records.', {}, source)
        runner = PlanRunner(service=AgentService(None), context=_context(),
            registry={tool.definition.name: tool}, command_capabilities={},
            budget=ContextBudget(None, 8192, 0), rounder=SimpleNamespace(social_route=False))
        arguments, observed = {'limit': 1}, []
        for index in range(21):
            step = {'id': 'page' + str(index), 'capability': tool.definition.name,
                    'arguments': arguments, 'depends_on': []}
            results = await runner.run({'steps': [step]})
            page = results[step['id']]
            self.assertNotIn('error', page)
            observed.extend(page['rows'])
            arguments = {'report_id': page['report_id'], 'cursor': page['next_cursor'], 'limit': 1}
        self.assertEqual(observed, expected)
        self.assertEqual(runner.tool_calls, 21)
        with patch('elbow_helper.agent.engine.budgets.MAX_TOOL_CALLS', 21):
            step = {**step, 'id': 'over_limit', 'arguments': {'report_id': page['report_id'], 'view': 'count'}}
            result = (await runner.run({'steps': [step]}))['over_limit']
        self.assertEqual(result['error'], 'The request reached its lookup limit.')
        self.assertEqual(runner.tool_calls, 21)


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
