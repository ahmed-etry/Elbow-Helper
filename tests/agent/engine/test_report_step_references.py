"""A bare step ID in a report field means that step's report; other unknown strings fail the plan check."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from elbow_helper.agent.engine.budgets import ContextBudget
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.steps import PlanRunner
from elbow_helper.agent.plan.checker import check_plan, step_dependencies
from agent.engine.test_agent_plan_flow import _context
from agent.engine.test_pending_report_retention import _plan, _report_size, _step, _tools

BARE = {'step': 'cohort', 'path': ['report_id']}


def _bare_plan(reference='cohort'):
    plan = _plan()
    plan['steps'][3]['arguments'] = {'cohort_ref': reference}
    return plan


class ReportStepReferenceTests(unittest.IsolatedAsyncioTestCase):
    def _runner(self, context):
        return PlanRunner(service=AgentService(None), context=context, registry=_tools(),
                          command_capabilities={}, budget=ContextBudget(None, 8192, 0),
                          rounder=SimpleNamespace(social_route=False))

    def test_bare_step_id_becomes_a_reference_and_a_dependency(self):
        plan = _bare_plan()
        check = check_plan(plan, _tools(), report_ids=set())
        self.assertTrue(check.ok, check.error)
        measure = plan['steps'][3]
        self.assertEqual(measure['arguments']['cohort_ref'], BARE)
        self.assertEqual(step_dependencies({**measure, 'depends_on': []}), ('cohort',))

    def test_bare_step_id_is_also_found_without_report_ids(self):
        plan = _bare_plan()
        self.assertTrue(check_plan(plan, _tools()).ok)
        self.assertEqual(plan['steps'][3]['arguments']['cohort_ref'], BARE)

    def test_invented_reference_string_is_refused_with_the_form_to_use(self):
        plan = _bare_plan('$cohort.report_id')
        check = check_plan(plan, _tools(), report_ids=set())
        self.assertFalse(check.ok)
        self.assertEqual(check.step_id, 'measure')
        self.assertIn('{"step": "<id>", "path": ["report_id"]}', check.error)
        self.assertIn('cohort_ref', check.error)

    def test_report_id_field_is_checked_the_same_way(self):
        plan = {'goal': 'Page a report', 'effort': 'low', 'output': 'text', 'steps': [
            _step('page', 'read_filler', [], {'report_id': 'invented'})]}
        self.assertFalse(check_plan(plan, _tools(), report_ids=set()).ok)
        self.assertTrue(check_plan(plan, _tools(), report_ids={'invented'}).ok)

    def test_report_held_in_this_request_still_works(self):
        plan = _bare_plan('abc123')
        check = check_plan(plan, _tools(), report_ids={'abc123'})
        self.assertTrue(check.ok, check.error)
        self.assertEqual(plan['steps'][3]['arguments']['cohort_ref'], 'abc123')

    def test_later_step_id_is_not_an_earlier_reference(self):
        plan = _bare_plan('later')
        plan['steps'].append(_step('later', 'read_filler'))
        check = check_plan(plan, _tools(), report_ids=set())
        self.assertFalse(check.ok)
        self.assertIn('earlier or completed step', check.error)

    def test_completed_step_id_resolves(self):
        completed = {'cohort': _step('cohort', 'read_member_cohort')}
        plan = {'goal': 'Measure', 'effort': 'low', 'output': 'text', 'steps': [
            _step('measure', 'read_dependent', [], {'cohort_ref': 'cohort'})]}
        self.assertTrue(check_plan(plan, _tools(), completed_steps=completed, report_ids=set()).ok)
        self.assertEqual(plan['steps'][0]['arguments']['cohort_ref'], BARE)

    async def test_dependent_read_gets_the_cohort_when_named_by_step_id(self):
        context = _context()
        results = await self._runner(context).run(_bare_plan())
        self.assertNotIn('error', results['measure'])
        self.assertEqual([row['member_id'] for row in results['measure']['rows']], [1, 2, 3])

    async def test_cohort_named_by_step_id_stays_retained_while_pending(self):
        size = await _report_size()
        context = _context()
        with patch('elbow_helper.agent.reports.base.MAX_REPORT_PAYLOAD_BYTES', int(size * 2.5)):
            results = await self._runner(context).run(_bare_plan())
        self.assertNotIn('error', results['measure'])
        self.assertEqual(len(results['measure']['rows']), 3)
        self.assertNotIn(results['first']['report_id'], context.state.reports)
        self.assertEqual(context.state.unkept_reports, set())

    async def test_real_retained_report_id_resolves_through_the_runner(self):
        context = _context()
        runner = self._runner(context)
        first = await runner.run({'goal': 'Read', 'effort': 'low', 'output': 'text',
                                  'steps': [_step('cohort', 'read_member_cohort')]})
        identifier = first['cohort']['report_id']
        plan = {'goal': 'Measure', 'effort': 'low', 'output': 'text', 'steps': [
            _step('measure', 'read_dependent', [], {'cohort_ref': identifier})]}
        self.assertTrue(check_plan(plan, _tools(), report_ids={*context.state.reports}).ok)
        second = await runner.run(plan)
        self.assertEqual(len(second['measure']['rows']), 3)

    async def test_lost_report_still_says_read_it_again(self):
        from elbow_helper.agent.families.projections import cohort_selection
        selection = await cohort_selection(_context(), 'was-held', 'member_ids')
        self.assertIn('Read it again', selection['error'])
