"""Synthetic member-facing limits preserve battle dates and omit polling diagnostics."""

import json
import unittest
from elbow_helper.agent.plan.results import model_result, model_view


class MemberLimitTests(unittest.TestCase):
    def test_dates_replace_polling_diagnostics_in_model_view(self):
        payload = {"history_coverage": {"status": "partial", "reasons": ["failed_battle_log_polls"]},
                   "flags": {"status": "partial", "limits": ["failed_battle_log_polls"]}, "accounts": [{
            "player_tag": "#PYY", "first_observed_at": "2026-03-01", "last_observed_at": "2026-03-10",
            "daily": [{"date": "2026-02-27", "attacks_used": 2, "defenses": 1}],
            "failed_battle_log_polls": 6, "successful_battle_log_polls": 160}]}
        view = model_view(model_result(payload))
        self.assertNotIn("polls", json.dumps(view))
        self.assertEqual(view["member_limits"][1]["available_battles_from"], "2026-02-27")
        self.assertIn("missing", view["member_limits"][0])
        self.assertEqual(payload["accounts"][0]["failed_battle_log_polls"], 6)

    def test_battle_coverage_returns_member_meaningful_range(self):
        payload = {"items": [{"tag": "#PYY", "coverage": {"available_range": {
            "oldest": "2026-03-01", "newest": "2026-03-10"}}}]}
        self.assertEqual(model_result(payload)["member_limits"][0]["history_from"], "2026-03-01")

    def test_empty_history_is_distinct_from_partial_history(self):
        view = model_result({"history_coverage": {"status": "empty"}, "accounts": []})
        self.assertIn("No ranked battle history", view["member_limits"][0])
