"""The approved social reply policy appears once and separates text emoji from reactions."""

import unittest

from elbow_helper.agent.prompts import PERSONA_RULES, DATA_RULES
from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.plan.format import system_instructions


class GifInstructionTests(unittest.TestCase):
    def test_approved_policy_is_present_once_without_link_pasting_instructions(self):
        policy = "Text alone is valid for social replies. Reactions and GIFs are optional; choose for the exchange."
        instructions = system_instructions(build_agent_tools(), social=True)
        self.assertEqual(instructions.count(policy), 1)
        self.assertNotIn("put it alone on the last line", instructions)
        self.assertNotIn("never alone", instructions)
        self.assertNotIn("find_gif", DATA_RULES)
        self.assertIn("Use emoji purposefully", PERSONA_RULES)
        self.assertIn("A selected image is the whole reply", PERSONA_RULES)
        self.assertNotIn("reaction or GIF can be the whole response", PERSONA_RULES)
        self.assertNotIn("reaction emoji", PERSONA_RULES)
        self.assertNotIn("about one in four conversation replies", instructions)
        self.assertNotIn("Never send GIFs in consecutive replies of your own", instructions)
        self.assertIn("GIF selection needs no research", instructions)
