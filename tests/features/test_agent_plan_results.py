"""Model-facing lookup results keep data and bounded flags."""

import unittest

from elbow_helper.features.agent.plan.results import model_result


class ModelResultTests(unittest.TestCase):
    def test_complete_result_keeps_data_and_dates(self):
        result = model_result({"rows": [{"value": 1}], "complete": True},
                              coverage_dates={"after": "2026-01-01T00:00:00Z"})
        self.assertEqual(result["rows"], [{"value": 1}])
        self.assertEqual(result["flags"], {
            "status": "complete", "truncated": False,
            "coverage_dates": {"after": "2026-01-01T00:00:00Z"},
        })

    def test_failure_and_limits_are_flags_without_caveat_sentences(self):
        result = model_result({
            "error": "That source is not accessible.",
            "limitations": ["The selected page is incomplete."],
            "rows": [],
        })
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["flags"], {
            "status": "failed", "truncated": False,
            "limits": ["access_denied", "partial"],
        })
        self.assertNotIn("error", result)
        self.assertNotIn("limitations", result)

    def test_truncation_marks_partial_result(self):
        result = model_result({"rows": [], "complete": True}, truncated=True)
        self.assertEqual(result["flags"]["status"], "partial")
        self.assertTrue(result["flags"]["truncated"])
