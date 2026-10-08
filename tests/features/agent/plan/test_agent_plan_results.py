"""Model-facing lookup results keep data and bounded flags."""

import unittest
import json
from copy import deepcopy
from dataclasses import asdict

from elbow_helper.features.agent.plan.results import (
    compact_result, model_view, model_result, result_handler, plan_feedback,
)
from elbow_helper.features.agent.plan.executor import resolve_arguments
from elbow_helper.features.cwl.queries import CwlPerformanceRow
from elbow_helper.features.agent.engine.registry import build_agent_tools
from unittest.mock import AsyncMock


class ModelResultTests(unittest.TestCase):
    def test_long_homogeneous_lists_use_first_record_column_order(self):
        rows = [{"target_id": index, "details": {"values": [index, None]}, "enabled": True}
                for index in range(10)]
        rows[1] = dict(reversed(list(rows[1].items())))
        original = deepcopy(rows)
        view = compact_result({"players": rows})
        self.assertEqual(view["players"]["columns"], ["target_id", "details", "enabled"])
        self.assertEqual(view["players"]["rows"][1], [1, {"values": [1, None]}, True])
        self.assertEqual(rows, original)
        self.assertEqual(json.loads(json.dumps(view)), view)

    def test_short_and_mixed_lists_keep_their_shape(self):
        for rows in ([{"value": index} for index in range(9)],
                     [{"value": index} for index in range(9)] + [{"other": 9}],
                     [{"value": index} for index in range(9)] + [None]):
            with self.subTest(rows=rows):
                self.assertEqual(compact_result({"rows": rows}), {"rows": rows})

    def test_compaction_leaves_wildcard_and_index_references_on_payload(self):
        payload = {"players": [{"target_id": index + 101} for index in range(10)]}
        view = compact_result(payload)
        self.assertIn("columns", view["players"])
        arguments = {"targets": {"step": "read", "path": ["players", "*", "target_id"]},
                     "first": {"step": "read", "path": ["players", 0, "target_id"]}}
        self.assertEqual(resolve_arguments(arguments, {"read": payload}),
                         {"targets": list(range(101, 111)), "first": 101})

    def test_400_cwl_like_rows_shrink_by_at_least_half(self):
        payload = synthetic_cwl_payload()
        before = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        after = len(json.dumps(compact_result(payload), ensure_ascii=False, separators=(",", ":")))
        self.assertLessEqual(after, before // 2, (before, after))
        self.assertEqual(len(compact_result(payload)["players"]["rows"]), 400)

    def test_repeated_page_summaries_are_omitted_only_from_model_view(self):
        summaries = {}
        payload = {"report_id": "synthetic-report", "summary": {"total": 20},
                   "clan_seasons": [{"season": "2026-01", "attacks": 140}],
                   "players": [{"target_id": index} for index in range(10)],
                   "next_offset": 10, "flags": {"status": "partial"}}
        first = model_view(payload, summaries=summaries, row_fields={"players"})
        self.assertEqual(first["summary"], payload["summary"])
        later = deepcopy(payload)
        later.update(players=[{"target_id": index} for index in range(10, 20)],
                     next_offset=None, flags={"status": "complete"})
        second = model_view(later, summaries=summaries, row_fields={"players"})
        self.assertEqual(set(second), {"report_id", "players", "next_offset", "flags"})
        self.assertIn("summary", later)
        later["summary"]["total"] = 21
        self.assertIn("summary", model_view(later, summaries=summaries, row_fields={"players"}))
        later["report_id"] = "other-synthetic-report"
        self.assertIn("summary", model_view(later, summaries=summaries, row_fields={"players"}))
        self.assertIn("summary", model_view(payload, summaries={}, row_fields={"players"}))

    def test_complete_result_keeps_data_and_dates(self):
        result = model_result({"rows": [{"value": 1}], "complete": True})
        self.assertEqual(result["rows"], [{"value": 1}])
        self.assertEqual(result["flags"], {
            "status": "complete", "truncated": False,
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
        self.assertEqual(result["error"], "That source is not accessible.")
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

    def test_plan_feedback_preserves_rule_and_offered_values_as_data(self):
        result = plan_feedback("Use the registered value.", step_id="second", offered=["202"])
        self.assertEqual(result["flags"], {"status": "refused", "rule": "use_the_registered_value"})
        self.assertEqual(result["step_id"], "second")
        self.assertEqual(result["offered"], ["202"])
        self.assertEqual(result["error"], "Use the registered value.")

    def test_every_page_cursor_marks_the_model_view_partial(self):
        for field, value in (("next_offset", 7), ("next_cursor", "synthetic-cursor")):
            with self.subTest(field=field):
                result = model_result({"rows": [7], "complete_snapshot": True, field: value})
                self.assertEqual(result["flags"]["status"], "partial")
                self.assertTrue(result["complete_snapshot"])
                self.assertEqual(result[field], value)
                self.assertEqual(model_result(result), result)


class ResultHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_boundary_returns_flags_for_errors(self):
        handler = AsyncMock(return_value={"error": "The source is not configured."})
        result = await result_handler(handler)(None, {"value": 7})
        handler.assert_awaited_once_with(None, {"value": 7})
        self.assertEqual(result, {"error": "The source is not configured.",
                                  "flags": {"status": "failed", "truncated": False,
                                           "limits": ["missing_setup"]}})


def synthetic_cwl_payload():
    return {"players": [asdict(CwlPerformanceRow(
        season="2026-01", clan_code="SYN", league="Synthetic League",
        profile_key="synthetic_profile", player_tag=f"#SYN{index}",
        player_name=f"Synthetic player {index}", townhall=18, wars=7, attacks=7,
        attacks_expected=7, stars=21, average_destruction=100.0, score=21.0,
        rank=index + 1, rank_total=400, multi_season_score=21.0,
        multi_season_rank=index + 1, multi_season_rank_total=400,
    )) for index in range(400)]}
