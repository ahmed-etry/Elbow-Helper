"""Both family paging boundaries preserve complete records and live cursors."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from elbow_helper.agent.engine.budgets import ContextBudget
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.steps import PlanRunner
from elbow_helper.agent.engine.tool_call import bound_tool_result
from elbow_helper.agent.engine.cohort_evidence import cohort_writer_evidence
from elbow_helper.agent.reads.snapshots import family_tool
from elbow_helper.agent.plan.results import model_result, model_view
from tests.agent.engine.test_agent_plan_flow import _context


def decode_rows(value):
    return [dict(zip(value['columns'], row)) for row in value['rows']] if isinstance(value, dict) else value


class FamilyPagingRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_participation_survives_an_indivisible_row_or_summary(self):
        # The live named-member read saved complete rows, then refused its first planner page.
        expected = [{'member_id': member, 'family_wars': [
            {'player_tag': f'#P{account}', 'regular': {'attacks_used': 0, 'attacks_expected': 0, 'missed': 0},
             'cwl': {'attacks_used': 0, 'attacks_expected': 0, 'missed': 0}}
            for account in range(160)]} for member in range(12)]
        for summary in ({}, {'record_span': {'accounts': expected}}):
            async def source(context, arguments):
                return {'rows': expected, **summary}
            tool = family_tool('read_war_participation', 'Read participation.', {}, source, whole=True)
            context = _context()
            runner = PlanRunner(service=AgentService(None), context=context,
                registry={tool.definition.name: tool}, command_capabilities={},
                budget=ContextBudget(None, 8192, 0), rounder=SimpleNamespace(social_route=False))
            result = await runner.run({'steps': [{'id': 'wars', 'capability': tool.definition.name,
                                                  'arguments': {}, 'depends_on': []}]})
            page = result['wars']
            self.assertNotIn('error', page)
            self.assertEqual((page['returned_rows'], page['total_rows'], page['next_offset']), (0, 12, 0))
            view = json.dumps(model_view(model_result(page)), ensure_ascii=False, separators=(',', ':'))
            bounded = json.loads(bound_tool_result(view, 16000))
            self.assertEqual((bounded['report_id'], bounded['total_rows'], bounded['returned_rows']),
                             (page['report_id'], 12, 0))
            written = await cohort_writer_evidence(context, runner.model_results_for(result),
                                                   snapshots=runner.report_snapshots)
            constants = written['wars'].get('row_rendering', {}).get('rows', {}).get('same_on_every_row', {})
            actual = [{**constants, **row} for row in decode_rows(written['wars']['rows'])]
            self.assertEqual([row['member_id'] for row in actual], [row['member_id'] for row in expected])
            for row, wanted in zip(actual, expected):
                self.assertEqual(decode_rows(row['family_wars']), wanted['family_wars'])

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
