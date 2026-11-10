"""A judgement answer carries a recommendation only when the request asks what to do or to choose."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.agent.engine.assessment import DECISION_ADVICE_INSTRUCTION, DECISION_ANSWER_INSTRUCTION
from elbow_helper.agent.engine.recommended_actions import prepare_recommended_actions
from elbow_helper.agent.plan.checker import check_plan
from tests.agent.engine import test_request_cost as cost


def decision(**overrides):
    payload = {'intent': 'Count the active members', 'intent_source': 'request',
               'discussion_message_ids': [], 'settled_choices': [], 'open_choices': [],
               'reply': 'Synthetic counts.', 'question': ''}
    return json.dumps({**payload, **overrides})


class PlanFieldTests(unittest.TestCase):

    def test_the_checker_accepts_only_a_boolean(self):
        plan = {'goal': 'Count', 'effort': 'high', 'output': 'text', 'reply': 'Counted.', 'steps': []}
        self.assertTrue(check_plan({**plan, 'advice': True}, {}).ok)
        self.assertTrue(check_plan({**plan, 'advice': False}, {}).ok)
        self.assertFalse(check_plan({**plan, 'advice': 'yes'}, {}).ok)


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_laid_out_reply_is_delivered_unchanged(self):
        reply = ('**Synthetic result**\n\n- First group\n  - Supporting detail\n'
                 '- Second group\n\n**Next step**\n1. Review the group.\n2. Choose a session.')
        for advice in (False, True):
            with self.subTest(advice=advice):
                flow = self.flow(decision(reply=reply), advice=advice)
                flow._repair_decision_format = AsyncMock()
                self.assertEqual(await flow.run(), reply)
                flow._repair_decision_format.assert_not_awaited()

    def test_only_advice_instructions_request_a_next_step(self):
        self.assertIn('Answer without advice.', DECISION_ANSWER_INSTRUCTION)
        self.assertNotIn('In reply, answer the requested next step', DECISION_ANSWER_INSTRUCTION)
        self.assertIn('In reply, answer the requested next step', DECISION_ADVICE_INSTRUCTION)
        self.assertNotIn('Answer without advice.', DECISION_ADVICE_INSTRUCTION)

    async def test_an_unresolved_reply_missing_its_question_is_repaired_before_delivery(self):
        reply = 'Synthetic counts.\n\n- Use the first session if it fits.\n\nWhich session?'
        draft = decision(intent_source='unresolved', open_choices=['Which session'],
                         question='Which session?')
        repaired = decision(intent_source='unresolved', open_choices=['Which session'],
                            question='Which session?', reply=reply)
        flow = self.flow(draft, advice=True)
        flow._repair_decision_format = AsyncMock(return_value=SimpleNamespace(content=repaired))
        self.assertEqual(await flow.run(), reply)
        flow._repair_decision_format.assert_awaited_once()

    def flow(self, content, *, advice):
        flow, _, _, _ = cost.RequestCostTests().flow([])
        flow.assessment_requested = True
        flow.advice_requested = advice
        flow.rounder.final_attempted = True
        flow._run = AsyncMock(return_value=content)
        return flow


    async def test_a_decision_without_requested_advice_prepares_no_action(self):
        flow = self.flow('', advice=False)
        payload = json.loads(decision(recommended_actions=[{'capability': 'synthetic_change',
            'target': {'member_id': 51}, 'arguments': {'member_id': 51}}]))
        self.assertEqual(await prepare_recommended_actions(flow, payload), ())


if __name__ == '__main__':
    unittest.main()
