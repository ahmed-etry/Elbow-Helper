"""Open choices belong to unresolved requests; a refused read keeps one correction before the answer."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.agent.engine.assessment import parse_decision, without_new_choices
from tests.agent.engine.answer_helpers import decision_answer
from elbow_helper.agent.engine.budgets import ASSESSMENT_LIMITS, ContextBudget, SOCIAL_LIMITS
from elbow_helper.agent.engine.routing import ROUTE_OUTPUT_TOKENS
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
from tests.agent.engine import test_request_cost as cost


def decision(**overrides):
    payload = {'intent': 'Count the active members', 'intent_source': 'request',
               'discussion_message_ids': [], 'settled_choices': [], 'open_choices': [],
               'reply': 'Synthetic counts.', 'question': ''}
    return json.dumps({**payload, **overrides})


class DecisionChoiceTests(unittest.IsolatedAsyncioTestCase):
    def test_an_invented_open_choice_is_emptied_for_a_resolved_request(self):
        content = decision(open_choices=['Which threshold counts'], question='Which threshold?')
        for source in ('request', 'discussion'):
            with self.subTest(source=source):
                payload = decision(intent_source=source, discussion_message_ids=[7] if source == 'discussion' else [],
                                   open_choices=['Which threshold counts'], question='Which threshold?')
                parsed = parse_decision(payload)
                self.assertEqual((parsed['open_choices'], parsed['question']), ([], ''))
                self.assertEqual(decision_answer(payload, {7}), 'Synthetic counts.')
        self.assertEqual(decision_answer(content, set()), 'Synthetic counts.')

    def test_an_unresolved_request_keeps_its_one_question(self):
        content = decision(intent_source='unresolved', open_choices=['Which clan'], question='Which clan?',
                           reply='Synthetic counts.\n\nWhich clan?')
        self.assertEqual(parse_decision(content)['open_choices'], ['Which clan'])
        self.assertEqual(decision_answer(content, set()), 'Synthetic counts.\n\nWhich clan?')
        with self.assertRaises(ValueError):
            decision_answer(decision(intent_source='unresolved'), set())

    def test_an_unresolved_question_must_be_present_in_the_reply(self):
        for question in ('', '   ', 'Which clan?'):
            with self.subTest(question=question), self.assertRaises(ValueError):
                decision_answer(decision(intent_source='unresolved', question=question,
                                         reply='Synthetic counts.'), set())
        missing = json.loads(decision(intent_source='unresolved', reply='Which clan?'))
        del missing['question']
        with self.assertRaises(ValueError):
            decision_answer(json.dumps(missing), set())


    def test_a_repair_cannot_add_open_choices_the_draft_lacked(self):
        draft = decision(intent_source='unresolved', question='Which clan?', open_choices=['Which clan'])
        repaired = decision(intent_source='unresolved', question='Which clan?',
                            open_choices=['Which clan', 'Which threshold counts as bad enough'])
        kept = json.loads(without_new_choices(draft, repaired))
        self.assertEqual(kept['open_choices'], ['Which clan'])
        invented = json.loads(without_new_choices(decision(), repaired))
        self.assertEqual(invented['open_choices'], [])
        self.assertEqual(json.loads(without_new_choices('not json', repaired))['open_choices'], [])
        self.assertEqual(without_new_choices(draft, 'not json'), 'not json')


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
    def test_social_route_and_a_full_plan_leave_a_correction_and_final_answer(self):
        budget = ContextBudget(128_000, SOCIAL_LIMITS.round_output_tokens, 25_000,
                               final_answer_reserve=4096, request_limits=SOCIAL_LIMITS)
        budget.observe(AgentUsage(1000, ROUTE_OUTPUT_TOKENS), projected_input=1000)
        budget.observe(AgentUsage(25_000, SOCIAL_LIMITS.round_output_tokens), projected_input=25_000)
        self.assertEqual(budget.request_final_reserve, 4096)
        self.assertTrue(budget.request_can_research(30_000, correction=True))
        self.assertTrue(budget.can_continue_tools(30_000, result_reserve=8192))
        correction_output = budget.request_output_limit(30_000, allow_tools=True)
        self.assertGreaterEqual(correction_output, 1024)
        budget.observe(AgentUsage(30_000, correction_output), projected_input=30_000)
        self.assertGreaterEqual(budget.request_output_limit(30_000, allow_tools=False),
                                budget.request_final_reserve)
        self.assertFalse(budget.request_can_research(30_000, correction=True))

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
