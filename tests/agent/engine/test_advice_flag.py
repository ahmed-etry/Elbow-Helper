"""A judgement answer carries a recommendation only when the request asks what to do or to choose."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.engine.assessment import (
    DECISION_ADVICE_INSTRUCTION, DECISION_ANSWER_INSTRUCTION, DECISION_SCHEMA, decision_answer,
    decision_instruction, decision_schema,
)
from elbow_helper.agent.engine.recommended_actions import prepare_recommended_actions
from elbow_helper.agent.plan.checker import check_plan
from elbow_helper.agent.plan.format import plan_definition
from agent.engine import test_request_cost as cost


def decision(**overrides):
    payload = {'intent': 'Count the active members', 'intent_source': 'request',
               'discussion_message_ids': [], 'settled_choices': [], 'open_choices': [],
               'facts': 'Synthetic counts.', 'question': ''}
    return json.dumps({**payload, **overrides})


class SchemaTests(unittest.TestCase):
    def test_without_advice_the_decision_has_no_recommendation_slot(self):
        schema = decision_schema(False)
        self.assertNotIn('recommendation', schema['properties'])
        self.assertNotIn('recommendation', schema['required'])
        self.assertIn('recommended_actions', schema['properties'])
        self.assertIn('recommendation', DECISION_SCHEMA['properties'])
        self.assertIs(decision_schema(True), DECISION_SCHEMA)

    def test_without_advice_the_instruction_drops_the_recommendation_sentences(self):
        self.assertEqual(DECISION_ANSWER_INSTRUCTION, decision_instruction(False))
        self.assertEqual(DECISION_ADVICE_INSTRUCTION, decision_instruction(True))
        for text in ('Recommendation addresses the requested next step', 'stating the assumption it rests on',
                     'A request for advice permits a provisional\nrecommendation', 'facts, recommendation and question', '"recommendation"',
                     'Use recommended_actions'):
            self.assertIn(text, DECISION_ADVICE_INSTRUCTION)
            self.assertNotIn(text, DECISION_ANSWER_INSTRUCTION)
        self.assertIn('facts and question', DECISION_ANSWER_INSTRUCTION)
        for text in (DECISION_ANSWER_INSTRUCTION, DECISION_ADVICE_INSTRUCTION):
            self.assertNotRegex(text, r'@[A-Z_]+@')
        self.assertLess(len(DECISION_ANSWER_INSTRUCTION), len(DECISION_ADVICE_INSTRUCTION))


class ValidationTests(unittest.TestCase):
    def test_a_recommendation_is_dropped_unless_advice_was_requested(self):
        self.assertEqual(decision_answer(decision(recommendation='Move the quiet group.'), set(), advice=False),
                         'Synthetic counts.')
        self.assertEqual(decision_answer(decision(recommendation='Move the quiet group.'), set(), advice=True),
                         'Synthetic counts.\n\nMove the quiet group.')

    def test_a_decision_without_the_slot_renders_its_facts(self):
        self.assertEqual(decision_answer(decision(), set(), advice=False), 'Synthetic counts.')
        with self.assertRaises(ValueError):
            decision_answer(decision(), set(), advice=True)

    def test_open_choices_still_end_with_the_question(self):
        content = decision(intent_source='unresolved', question='Which clan?', open_choices=['Which clan'])
        self.assertEqual(decision_answer(content, set(), advice=False), 'Synthetic counts.\n\nWhich clan?')
        self.assertEqual(decision_answer(decision(question='Which clan?'), set(), advice=False),
                         'Synthetic counts.')


class PlanFieldTests(unittest.TestCase):
    def test_the_plan_offers_a_boolean_advice_field_described_for_the_planner(self):
        property_ = plan_definition({}).parameters['properties']['advice']
        self.assertEqual(property_['type'], 'boolean')
        self.assertNotIn('advice', plan_definition({}).parameters['required'])
        self.assertIn('asks what to do or to choose', property_['description'])
        self.assertIn('facts, stats or lists', property_['description'])

    def test_the_checker_accepts_only_a_boolean(self):
        plan = {'goal': 'Count', 'effort': 'high', 'output': 'text', 'reply': 'Counted.', 'steps': []}
        self.assertTrue(check_plan({**plan, 'advice': True}, {}).ok)
        self.assertTrue(check_plan({**plan, 'advice': False}, {}).ok)
        self.assertFalse(check_plan({**plan, 'advice': 'yes'}, {}).ok)


class FlowTests(unittest.IsolatedAsyncioTestCase):
    def flow(self, content, *, advice):
        flow, _, _, _ = cost.RequestCostTests().flow([])
        flow.assessment_requested = True
        flow.advice_requested = advice
        flow.rounder.final_attempted = True
        flow._run = AsyncMock(return_value=content)
        return flow

    async def test_the_writer_is_given_the_instruction_that_matches_the_plan(self):
        self.assertTrue(self.flow('', advice=False)._decision_instruction().startswith(DECISION_ANSWER_INSTRUCTION))
        self.assertTrue(self.flow('', advice=True)._decision_instruction().startswith(DECISION_ADVICE_INSTRUCTION))
        self.assertFalse(cost.RequestCostTests().flow([])[0].advice_requested)

    async def test_a_facts_answer_has_no_recommendation(self):
        flow = self.flow(decision(), advice=False)
        self.assertEqual(await flow.run(), 'Synthetic counts.')

    async def test_a_recommendation_in_a_facts_answer_is_dropped_without_a_repair(self):
        flow = self.flow(decision(recommendation='Move the quiet group.'), advice=False)
        flow._repair_decision_format = AsyncMock()
        self.assertEqual(await flow.run(), 'Synthetic counts.')
        flow._repair_decision_format.assert_not_awaited()

    async def test_an_advice_answer_keeps_its_recommendation(self):
        flow = self.flow(decision(recommendation='Move the quiet group.'), advice=True)
        self.assertEqual(await flow.run(), 'Synthetic counts.\n\nMove the quiet group.')

    async def test_member_plan_limits_still_apply_to_the_advice_field(self):
        plan = {'goal': 'Count', 'effort': 'high', 'output': 'text', 'steps': [], 'advice': True}
        with patch('elbow_helper.agent.engine.flow.leadership_asker', return_value=False):
            limited = self.flow('', advice=False)._limit_plan(plan)
        self.assertNotIn('advice', limited)
        with patch('elbow_helper.agent.engine.flow.leadership_asker', return_value=True):
            self.assertEqual(self.flow('', advice=False)._limit_plan(plan), plan)

    async def test_a_decision_without_the_slot_prepares_no_action(self):
        flow = self.flow('', advice=False)
        payload = json.loads(decision(recommended_actions=[{'capability': 'synthetic_change',
            'target': {'member_id': 51}, 'arguments': {'member_id': 51}}]))
        self.assertEqual(await prepare_recommended_actions(flow, payload), ())


if __name__ == '__main__':
    unittest.main()
