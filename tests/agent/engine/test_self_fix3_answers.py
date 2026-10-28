"""Saved failure shapes retain complete answers within shared request limits."""
import unittest
from unittest.mock import Mock

from elbow_helper.agent.engine.budgets import FAMILY_LIMITS, RequestLimits
from elbow_helper.agent.knowledge.answers import scope_reply_issue
from elbow_helper.agent.wording import AGENT_ANSWER_UNFINISHED
from agent.engine import test_request_cost as cost


class WriterRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def flow(self):
        flow, _, _, _ = cost.RequestCostTests.flow(self, [])
        flow.budget.request_limits = FAMILY_LIMITS
        flow.budget.spent_rounds = 2  # Route and checked plan.
        flow.completed_results = {'roles': {'roles': [], 'matched_count': 0}}
        flow.result_views = dict(flow.completed_results)
        return flow

    async def test_saved_th18_shape_has_one_answer_only_repair(self):
        flow = self.flow()
        draft = 'The role search came back empty, so no TH18 role is confirmed.'
        final = 'No TH18 role is confirmed.'
        session = cost.Session([cost.response(draft), cost.response(final)], [])
        flow.final_session_factory = Mock(return_value=session)
        answer = await flow._compact_reply()
        self.assertTrue(scope_reply_issue(flow.context, answer))
        self.assertEqual(await flow._checked_scope_reply(answer), final)
        self.assertEqual(flow.budget.spent_rounds, 4)
        self.assertTrue(all(not call[1] for call in session.calls))
        self.assertEqual(flow.runner.tool_calls, 0)
        self.assertFalse(flow.budget.request_can_research(1000))

    async def test_empty_writer_retries_once_without_research(self):
        flow = self.flow()
        session = cost.Session([cost.response(''), cost.response('No TH18 role is confirmed.')], [])
        flow.final_session_factory = Mock(return_value=session)
        self.assertEqual(await flow._compact_reply(), 'No TH18 role is confirmed.')
        self.assertEqual(flow.budget.spent_rounds, 4)
        self.assertTrue(all(not call[1] for call in session.calls))

    async def test_exhausted_style_repair_keeps_finished_answer_without_extra_call(self):
        flow = self.flow()
        flow.budget.request_limits = RequestLimits(3, 180_000, 20_000, 8192)
        draft = 'The role search came back empty, so no TH18 role is confirmed.'
        session = cost.Session([cost.response(draft)], [])
        flow.final_session_factory = Mock(return_value=session)
        answer = await flow._compact_reply()
        self.assertEqual(await flow._checked_scope_reply(answer), draft)
        self.assertEqual(len(session.calls), 1)

    async def test_style_fallback_does_not_excuse_invented_private_claim(self):
        flow = self.flow()
        flow.budget.spent_rounds = FAMILY_LIMITS.rounds
        draft = 'The search came back empty. The names are private-only.'
        self.assertEqual(await flow._checked_scope_reply(draft), AGENT_ANSWER_UNFINISHED)

    def test_family_planning_reserves_dependent_read_and_answer_repair(self):
        flow = self.flow()
        self.assertTrue(flow.budget.request_can_research(1000))
        flow.budget.spent_rounds = 3
        self.assertFalse(flow.budget.request_can_research(1000))
