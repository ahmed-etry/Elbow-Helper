"""The approved social reply policy appears once and separates text emoji from reactions."""

import unittest

from elbow_helper.features.agent.prompts import BEHAVIOR_RULES, DATA_RULES
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.plan.format import system_instructions


class GifInstructionTests(unittest.TestCase):
    def test_approved_policy_is_present_once_without_link_pasting_instructions(self):
        policy = (
            "This server talks in reactions and GIFs as much as words. "
            "React instead of replying when a reaction says it: thanks, acknowledgements, "
            "a joke landing, something hype. In banter, or when someone sends you a GIF, "
            "a GIF can be the whole reply. Keep data, decisions and anything serious in words, "
            "and don't react or GIF on every message."
        )
        instructions = system_instructions(build_agent_tools())
        self.assertEqual(instructions.count(policy), 1)
        self.assertNotIn("put it alone on the last line", instructions)
        self.assertNotIn("find_gif", DATA_RULES)
        self.assertIn("Use emoji in text rarely, never decoratively", BEHAVIOR_RULES)
        self.assertNotIn("reaction emoji", BEHAVIOR_RULES)
