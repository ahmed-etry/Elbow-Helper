"""Compare identical synthetic requests through lookup and full-context routes."""

import unittest
from unittest.mock import patch

from features.agent.engine.cost_scenarios import measure


class CostScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def test_smaller_context_preserves_answers_and_full_research(self):
        with (patch("elbow_helper.features.agent.engine.service.simple_lookup", return_value=False),
              patch("elbow_helper.features.agent.engine.service.lightweight_request", return_value=False)):
            before = await measure()
        after = await measure()
        for name in before:
            self.assertEqual(after[name]["answer"], before[name]["answer"])
            self.assertLessEqual(after[name]["rounds"], before[name]["rounds"])
        for name in ("count", "extraction", "correction"):
            self.assertLess(after[name]["input_tokens_estimated"],
                            before[name]["input_tokens_estimated"] * 0.5)
            self.assertEqual(after[name]["first_effort"], "low")
        self.assertEqual(after["count"]["rounds"], 1)
        self.assertEqual(before["count"]["rounds"], 2)
        self.assertEqual(after["multi-source"], before["multi-source"])
