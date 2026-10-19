"""The approved social reply policy appears once and separates text emoji from reactions."""

import unittest

from elbow_helper.features.agent.prompts import PERSONA_RULES, DATA_RULES
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.plan.format import system_instructions


class GifInstructionTests(unittest.TestCase):
    def test_approved_policy_is_present_once_without_link_pasting_instructions(self):
        policy = (
            "React alongside a text or GIF reply, never alone. Use listed server emoji: "
            "nodCat for agreement. Prefer find_gif over words for banter, celebration or "
            "mock outrage when it fits. Plan at low effort; a GIF can be the whole reply, "
            "or keep text brief. No GIFs on factual answers, decisions or serious help. "
            "Don't react or GIF on every message."
        )
        instructions = system_instructions(build_agent_tools())
        self.assertEqual(instructions.count(policy), 1)
        self.assertNotIn("put it alone on the last line", instructions)
        self.assertNotIn("find_gif", DATA_RULES)
        self.assertIn("Use emoji sparingly and purposefully", PERSONA_RULES)
        self.assertIn("A fitting GIF can be the whole reply", PERSONA_RULES)
        self.assertNotIn("reaction or GIF can be the whole response", PERSONA_RULES)
        self.assertNotIn("reaction emoji", PERSONA_RULES)
