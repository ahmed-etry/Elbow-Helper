"""Synthetic cost replay preserves answers through the full cached prefix."""

from features.agent.engine.helpers import LegacyCatalogueMixin

import unittest

from features.agent.engine.cost_scenarios import measure


class CostScenarioTests(LegacyCatalogueMixin, unittest.IsolatedAsyncioTestCase):
    async def test_all_requests_start_with_the_same_full_prefix(self):
        measured = await measure()
        self.assertEqual(len({item["system_characters"] for item in measured.values()}), 1)
        for item in measured.values():
            self.assertEqual(item["first_effort"], "high")
            self.assertGreater(item["input_characters"], 0)
        self.assertEqual(measured["count"]["rounds"], 3)
        self.assertEqual(measured["extraction"]["rounds"], 3)
        self.assertEqual(measured["correction"]["rounds"], 3)
        self.assertEqual(measured["multi-source"]["rounds"], 4)
