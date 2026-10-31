"""A refused read keeps one correction round before the answer."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.agent.engine.budgets import ASSESSMENT_LIMITS
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
from agent.engine import test_request_cost as cost


class CorrectionRoundTests(unittest.IsolatedAsyncioTestCase):
    def assessment(self):
        content = json.dumps({'research_complete': True})
        flow, session, _, _ = cost.RequestCostTests().flow([AgentStep(content, (), AgentUsage(30000, 100))])
        flow.assessment_requested = True
        flow.budget.request_limits = ASSESSMENT_LIMITS
        # Twice this evidence estimate overruns the request allowance.
        flow.budget.final_input_estimate = 190_000
        flow.budget.spent_input = 26_000
        flow.plan = {'finish_after_reads': False}
        flow.decision = SimpleNamespace(rounds=(SimpleNamespace(tool_calls=()),))
        flow.new_evidence = True
        flow.result_views['read'] = {'value': 7}

        async def prepare(**kwargs):
            flow.budget.next_input_estimate = 30_000
            return kwargs.get('allow_more', False)

        flow._prepare_answer_session = AsyncMock(side_effect=prepare)
        flow._compact_reply = AsyncMock(return_value='Final answer.')
        return flow, session

    async def test_large_evidence_skips_review_when_every_read_succeeded(self):
        flow, session = self.assessment()
        answer, step = await flow._answer_round({'read': {'value': 7}})
        self.assertEqual((answer, step), ('Final answer.', None))
        flow._compact_reply.assert_awaited_once()
        self.assertEqual(session.calls, [])
        self.assertFalse(flow.correction_granted)

    async def test_an_invalid_arguments_read_keeps_one_correction_round_before_the_answer(self):
        for refused in ({'cause': 'invalid_arguments', 'error': 'Not CWL clans: SYN.'},
                        {'status': 'needs_input', 'missing': ['clan']}):
            with self.subTest(refused=refused):
                flow, session = self.assessment()
                answer, step = await flow._answer_round({'read': refused})
                self.assertEqual(answer, json.dumps({'research_complete': True}))
                flow._compact_reply.assert_not_awaited()
                self.assertEqual(len(session.calls), 1)
                self.assertTrue(session.calls[0][1])
                self.assertTrue(flow.correction_granted)

    async def test_the_correction_is_granted_once(self):
        flow, session = self.assessment()
        flow.correction_granted = True
        answer, step = await flow._answer_round(
            {'read': {'cause': 'invalid_arguments', 'error': 'Not CWL clans: SYN.'}})
        self.assertEqual(answer, 'Final answer.')
        flow._compact_reply.assert_awaited_once()
        self.assertEqual(session.calls, [])


class CorrectionBudgetTests(unittest.TestCase):
    def test_a_correction_ignores_the_writer_estimate_but_not_the_request_allowance(self):
        flow, _, _, _ = cost.RequestCostTests().flow([])
        budget = flow.budget
        budget.request_limits = ASSESSMENT_LIMITS
        budget.final_input_estimate = 190_000
        budget.spent_input = 26_000
        self.assertFalse(budget.request_can_research(30_000))
        self.assertTrue(budget.request_can_research(30_000, correction=True))
        budget.spent_input = ASSESSMENT_LIMITS.input_tokens
        self.assertFalse(budget.request_can_research(30_000, correction=True))
        budget.spent_input = 26_000
        budget.spent_rounds = ASSESSMENT_LIMITS.rounds
        self.assertFalse(budget.request_can_research(30_000, correction=True))


if __name__ == '__main__':
    unittest.main()
