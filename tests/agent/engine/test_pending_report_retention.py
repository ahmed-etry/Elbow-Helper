"""Reports that unfinished plan steps reference stay retained, or fail with a clear reason."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from elbow_helper.agent.engine.budgets import ContextBudget
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.steps import PlanRunner
from elbow_helper.agent.reads.snapshots import family_tool
from elbow_helper.agent.reads.projections import COHORT_REF, cohort_selection
from tests.agent.engine.test_agent_plan_flow import _context

ROWS = [{'member_id': index, 'pad': 'x' * 120} for index in range(1, 4)]


def _tools():
    async def rows(context, arguments):
        return {'rows': ROWS}

    async def dependent(context, arguments):
        selection = await cohort_selection(context, arguments['cohort_ref'], 'member_ids')
        if 'error' in selection:
            return selection
        return {'rows': [{'member_id': value} for value in selection['member_ids']]}

    tools = (family_tool('read_member_cohort', 'Read a synthetic cohort.', {}, rows),
             family_tool('read_filler', 'Read synthetic filler.', {}, rows),
             family_tool('read_dependent', 'Read per cohort.', {'cohort_ref': COHORT_REF}, dependent,
                         required=('cohort_ref',)))
    return {tool.definition.name: tool for tool in tools}


def _step(step_id, capability, depends_on=(), arguments=None):
    return {'id': step_id, 'capability': capability, 'depends_on': list(depends_on),
            'arguments': arguments or {}}


def _plan():
    return {'goal': 'Compare a cohort', 'effort': 'low', 'output': 'text', 'steps': [
        _step('cohort', 'read_member_cohort'),
        _step('first', 'read_filler'),
        _step('second', 'read_filler', ['first']),
        _step('measure', 'read_dependent', ['second'],
              {'cohort_ref': {'step': 'cohort', 'path': ['report_id']}}),
    ]}


async def _report_size():
    context = _context()
    await _tools()['read_filler'].handler(context, {})
    return next(iter(context.state.reports.values())).retained_bytes


class PendingReportRetentionTests(unittest.IsolatedAsyncioTestCase):
    def _runner(self, context):
        return PlanRunner(service=AgentService(None), context=context, registry=_tools(),
                          command_capabilities={}, budget=ContextBudget(None, 8192, 0),
                          rounder=SimpleNamespace(social_route=False))

    async def test_cohort_survives_later_reports_until_its_dependent_step_runs(self):
        size = await _report_size()
        context = _context()
        with patch('elbow_helper.agent.reports.base.MAX_REPORT_PAYLOAD_BYTES', int(size * 2.5)):
            results = await self._runner(context).run(_plan())
        self.assertNotIn('error', results['measure'])
        self.assertEqual([row['member_id'] for row in results['measure']['rows']], [1, 2, 3])
        self.assertNotIn(results['first']['report_id'], context.state.reports)
        self.assertEqual(context.state.unkept_reports, set())

    async def test_count_limit_also_keeps_the_referenced_cohort(self):
        context = _context()
        with patch('elbow_helper.agent.reports.base.MAX_REPORTS', 2):
            results = await self._runner(context).run(_plan())
        self.assertNotIn('error', results['measure'])
        self.assertEqual(len(results['measure']['rows']), 3)
        self.assertLessEqual(len(context.state.reports), 2)
        self.assertEqual(context.state.unkept_reports, set())

    async def test_cohort_that_cannot_be_kept_reports_it_was_too_large(self):
        size = await _report_size()
        context = _context()
        with patch('elbow_helper.agent.reports.base.MAX_REPORT_PAYLOAD_BYTES', int(size * 1.5)):
            results = await self._runner(context).run(_plan())
        identifier = results['cohort']['report_id']
        self.assertNotIn(identifier, context.state.reports)
        self.assertIn(identifier, context.state.unkept_reports)
        self.assertEqual(results['measure']['cause'], 'capacity')
        self.assertIn('too large to keep', results['measure']['error'])
        self.assertNotIn('Read it again', results['measure']['error'])

    async def test_evicted_report_without_a_pending_reference_is_still_unavailable(self):
        context = _context()
        selection = await cohort_selection(context, 'missing', 'member_ids')
        self.assertEqual(selection['cause'], 'report_unavailable')
