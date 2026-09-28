"""Model-facing lookup results keep data and bounded flags."""

import unittest

from elbow_helper.features.agent.plan.results import model_result, result_handler
from elbow_helper.features.agent.tools import build_agent_tools
from unittest.mock import AsyncMock


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

    def test_every_registered_handler_normalizes_results(self):
        for name, tool in build_agent_tools().items():
            with self.subTest(capability=name):
                self.assertTrue(tool.handler.normalizes_results)

    def test_flags_and_structured_limits_survive_repeated_normalization(self):
        data = {"rows": [{"note": "authored source text"}],
                "projection_note": {"basis": "observed_values", "verified": False},
                "flags": {"status": "partial", "truncated": True,
                          "limits": ["synthetic_limit"],
                          "coverage_dates": {"after": "2026-01-01"}}}
        result = model_result(data)
        self.assertEqual(result["rows"], data["rows"])
        self.assertEqual(result["projection_note"], data["projection_note"])
        self.assertEqual(result["flags"], data["flags"])
        self.assertEqual(model_result(result), result)

    def test_feature_flags_are_preserved(self):
        result = model_result({"flags": ["synthetic_data_flag"]})
        self.assertEqual(result["data_flags"], ["synthetic_data_flag"])


class ResultHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_boundary_returns_flags_for_errors(self):
        handler = AsyncMock(return_value={"error": "The source is not configured."})
        result = await result_handler(handler)(None, {"value": 7})
        handler.assert_awaited_once_with(None, {"value": 7})
        self.assertEqual(result, {"flags": {"status": "failed", "truncated": False,
                                           "limits": ["missing_setup"]}})
