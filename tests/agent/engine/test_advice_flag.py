"""A judgement answer carries a recommendation only when the request asks what to do or to choose."""
import json
import unittest
from unittest.mock import AsyncMock

from elbow_helper.agent.engine.recommended_actions import prepare_recommended_actions
from elbow_helper.agent.plan.checker import check_plan
from tests.agent.engine import test_request_cost as cost


def decision(**overrides):
    payload = {'intent': 'Count the active members', 'intent_source': 'request',
               'discussion_message_ids': [], 'settled_choices': [], 'open_choices': [],
               'facts': 'Synthetic counts.', 'question': ''}
    return json.dumps({**payload, **overrides})


class PlanFieldTests(unittest.TestCase):

    def test_the_checker_accepts_only_a_boolean(self):
        plan = {'goal': 'Count', 'effort': 'high', 'output': 'text', 'reply': 'Counted.', 'steps': []}
        self.assertTrue(check_plan({**plan, 'advice': True}, {}).ok)
        self.assertTrue(check_plan({**plan, 'advice': False}, {}).ok)
        self.assertFalse(check_plan({**plan, 'advice': 'yes'}, {}).ok)


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_requested_advice_controls_the_delivered_recommendation(self):
        for advice, expected in ((False, 'Synthetic counts.'),
                                 (True, 'Synthetic counts.\n\nMove the quiet group.')):
            with self.subTest(advice=advice):
                flow = self.flow(decision(recommendation='Move the quiet group.'), advice=advice)
                flow._repair_decision_format = AsyncMock()
                self.assertEqual(await flow.run(), expected)
                flow._repair_decision_format.assert_not_awaited()

    def flow(self, content, *, advice):
        flow, _, _, _ = cost.RequestCostTests().flow([])
        flow.assessment_requested = True
        flow.advice_requested = advice
        flow.rounder.final_attempted = True
        flow._run = AsyncMock(return_value=content)
        return flow


    async def test_a_decision_without_the_slot_prepares_no_action(self):
        flow = self.flow('', advice=False)
        payload = json.loads(decision(recommended_actions=[{'capability': 'synthetic_change',
            'target': {'member_id': 51}, 'arguments': {'member_id': 51}}]))
        self.assertEqual(await prepare_recommended_actions(flow, payload), ())


if __name__ == '__main__':
    unittest.main()
