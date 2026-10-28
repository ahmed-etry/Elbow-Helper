"""General banter grounding belongs once in the model instructions."""

import unittest

from elbow_helper.agent.engine.routing import SOCIAL_INSTRUCTION
from elbow_helper.agent.engine.social_reply import (
    SOCIAL_REPLY_INSTRUCTION, social_reply_definition,
)
from elbow_helper.agent.plan.format import system_instructions


class BanterGroundingTests(unittest.TestCase):
    def test_response_policy_distinguishes_a_contribution_from_repeating_the_setup(self):
        instructions = system_instructions({}, social=True)
        self.assertIn('The member has already supplied the setup', instructions)
        self.assertIn('drop it when the member changes or ends the bit', instructions)
        self.assertIn('Clearly invented comic consequences', instructions)
        self.assertIn('A greeting, agreement or celebration can be direct', instructions)
        self.assertNotIn('Examples of grounded banter:', instructions)
        self.assertNotIn('must make literal sense', instructions)
        description = social_reply_definition({}).parameters['properties']['text']['description']
        for prompt in (instructions, description):
            self.assertIn('Never open by repeating numbers, items or events from any member message, including earlier turns.', prompt)
        self.assertIn('Callbacks must add something, not recap the setup.', instructions)
        self.assertNotIn('gif_with_text', social_reply_definition({}).parameters['properties'])

    def test_general_rules_reach_both_permission_modes_once(self):
        rules = (
            "No unasked advice.",
            "Never add facts about the member's play that they did not state.",
            "Numbers in jokes must make game sense.",
            "If the stated mechanic is impossible, call out the mismatch briefly instead of endorsing it",
        )
        for actions in (False, True):
            for combo_due in (False, True):
                with self.subTest(actions=actions, combo_due=combo_due):
                    instructions = system_instructions({}, actions_enabled=actions, social=True)
                    social_instructions = "\n\n".join((
                        instructions, SOCIAL_INSTRUCTION, SOCIAL_REPLY_INSTRUCTION,
                    ))
                    description = social_reply_definition(
                        {"react_to_request": object()}, combo_due=combo_due,
                    ).parameters["properties"]["text"]["description"]
                    for rule in rules:
                        with self.subTest(rule=rule):
                            self.assertEqual(instructions.count(rule), 1)
                            self.assertEqual(social_instructions.count(rule), 1)
                            self.assertNotIn(rule, description)


if __name__ == "__main__":
    unittest.main()
