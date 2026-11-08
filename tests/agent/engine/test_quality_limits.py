"""Synthetic member-facing limits preserve battle dates and omit polling diagnostics."""

import unittest
from elbow_helper.agent.plan.results import model_result


class MemberLimitTests(unittest.TestCase):

    def test_battle_coverage_returns_member_meaningful_range(self):
        payload = {"items": [{"tag": "#PYY", "coverage": {"available_range": {
            "oldest": "2026-03-01", "newest": "2026-03-10"}}}]}
        self.assertEqual(model_result(payload)["member_limits"][0]["history_from"], "2026-03-01")

    def test_empty_history_is_distinct_from_partial_history(self):
        view = model_result({"history_coverage": {"status": "empty"}, "accounts": []})
        self.assertIn("No ranked battle history", view["member_limits"][0])
