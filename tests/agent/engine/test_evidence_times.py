"""Writer timestamp projection uses structured fields, never source prose."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from elbow_helper.agent.engine.answer_inputs import answer_evidence
from elbow_helper.agent.plan.results import compact_result


class EvidenceTimeTests(unittest.TestCase):
    def setUp(self):
        self.instant = datetime(2025, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
        self.epoch = int(self.instant.timestamp())
        self.readable = "2025-02-03 04:05:06 UTC"

    def test_all_families_format_nested_timestamp_fields_without_mutating_sources(self):
        raw = {
            "signup": {"family": "read_cwl_results", "rows": [
                {"member_id": self.epoch, "signed_up_ts": self.epoch, "townHallLevel": 17}]},
            "workflow": {"family": "read_recruitment_pipeline", "rows": [
                {"timeline": [{"created_at": self.epoch + 0.25}], "expected_end_ts": str(self.epoch)}]},
            "history": {"messages": [{"timestamp": self.epoch}],
                        "coverage": {"returned_range": {"oldest": self.epoch, "newest": self.epoch}}},
            "retained": {"older": {"rows": [{"started_ts": self.epoch, "duration_seconds": self.epoch}]}}
        }
        original = deepcopy(raw)
        result = answer_evidence(raw)
        self.assertEqual(result["signup"]["rows"], [
            {"member_id": self.epoch, "signed_up_ts": self.readable, "townHallLevel": 17}])
        self.assertEqual(result["workflow"]["rows"][0], {
            "timeline": [{"created_at": "2025-02-03 04:05:06.250000 UTC"}],
            "expected_end_ts": self.readable})
        self.assertEqual(result["history"]["messages"], [{"timestamp": self.readable}])
        self.assertEqual(result["history"]["coverage"]["returned_range"],
                         {"oldest": self.readable, "newest": self.readable})
        self.assertEqual(result["retained"]["older"]["rows"], [
            {"started_ts": self.readable, "duration_seconds": self.epoch}])
        self.assertEqual(raw, original)

    def test_compacted_rows_keep_columns_and_format_timestamp_cells_at_any_depth(self):
        rows = [{"member_id": index, "signed_up_ts": self.epoch,
                 "details": {"updated_at": self.epoch}, "score": self.epoch} for index in range(12)]
        raw = {"sample": compact_result({"rows": rows})}
        original = deepcopy(raw)
        table = answer_evidence(raw)["sample"]["rows"]
        self.assertEqual(table["columns"], list(rows[0]))
        self.assertEqual(table["rows"], [[index, self.readable, {"updated_at": self.readable}, self.epoch]
                                        for index in range(12)])
        self.assertEqual(raw, original)

    def test_non_temporal_values_and_source_strings_stay_exact(self):
        prose = f"signed_up_ts {self.epoch}; timestamp {self.epoch}"
        row = {"body": prose, "content": prose, "notes": prose, "name": str(self.epoch),
               "count": self.epoch, "remaining_seconds": self.epoch, "before": self.epoch,
               "after": self.epoch, "time": "18:30", "signed_up_ts": None,
               "timestamp": self.instant.isoformat()}
        self.assertEqual(answer_evidence({"sample": {"rows": [row]}}), {"sample": {"rows": [row]}})

    def test_limits_and_timestamp_arrays_are_projected_too(self):
        raw = {"sample": {"status": "needs_input", "choices": [
            {"opens_at": [self.epoch, self.epoch], "deadline_ts": self.epoch}]}}
        choice = answer_evidence(raw)["sample"]["choices"][0]
        self.assertEqual(choice, {"opens_at": [self.readable, self.readable], "deadline_ts": self.readable})

    def test_invalid_timestamps_become_unknown_and_booleans_stay_booleans(self):
        for value in (float("nan"), float("inf"), -float("inf"), 10**1000, "1e999"):
            with self.subTest(value_type=type(value).__name__):
                self.assertIsNone(answer_evidence({"sample": {"created_at": value}})["sample"]["created_at"])
        self.assertIs(answer_evidence({"sample": {"created_at": False}})["sample"]["created_at"], False)

    def test_utc_formatting_handles_epoch_zero_negative_values_and_aware_datetimes(self):
        raw = {"sample": {"created_ts": 0, "updated_at": -1,
            "observed_at": self.instant.astimezone(timezone(timedelta(hours=3))),
            "endTime": self.epoch, "ts_utc": str(self.epoch)}}
        self.assertEqual(answer_evidence(raw)["sample"], {
            "created_ts": "1970-01-01 00:00:00 UTC", "updated_at": "1969-12-31 23:59:59 UTC",
            "observed_at": self.readable, "endTime": self.readable, "ts_utc": self.readable})
