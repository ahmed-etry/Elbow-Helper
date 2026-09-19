from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock, patch

from elbow_helper.features.records.queries import LeadershipRecordQueries


def _record(record_id=2, member_id=42, **changes):
    value = {
        "id": record_id,
        "created_ts": 1_700_000_000 + record_id,
        "updated_ts": 1_700_000_100 + record_id,
        "status": "active",
        "member_id": member_id,
        "member_display": "Member",
        "category_key": "war",
        "incident_type_key": "war_missed_attacks",
        "note": "Missed both attacks.",
        "recorder_id": 99,
        "recorder_display": "Lead",
        "edited_by_id": None,
        "removed_ts": None,
    }
    value.update(changes)
    return value


class LeadershipRecordQueryTests(unittest.TestCase):
    def setUp(self):
        self.values = [_record(), _record(1, 84, category_key="communication",
                                         incident_type_key="communication_no_response",
                                         note="No response to follow-up.")]
        self.reader = MagicMock()
        self.reader.list.return_value = self.values
        self.queries = LeadershipRecordQueries(
            self.reader,
            clock=lambda: datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
        )

    def test_snapshot_projects_complete_active_fields_without_audit_state(self):
        original = deepcopy(self.values)
        snapshot = self.queries.active_snapshot()

        self.reader.list.assert_called_once_with(
            member_id=None, include_removed=False, limit=1001,
        )
        self.assertEqual([row.record_id for row in snapshot.records], [2, 1])
        self.assertEqual(snapshot.records[0].category_label, "War")
        self.assertEqual(snapshot.records[0].incident_type_label, "Missed Attack")
        self.assertEqual(snapshot.records[0].note, "Missed both attacks.")
        self.assertNotIn("removed", repr(snapshot))
        self.assertNotIn("recorder_id", repr(snapshot))
        self.assertEqual(self.values, original)

    def test_member_scope_and_malformed_or_removed_rows_fail_closed(self):
        self.reader.list.return_value = [_record(member_id=42)]
        snapshot = self.queries.active_snapshot(member_id=42)
        self.assertEqual(snapshot.member_id, 42)
        self.reader.list.assert_called_once_with(
            member_id=42, include_removed=False, limit=1001,
        )

        for changes in (
            {"status": "removed"},
            {"member_id": 84},
            {"category_key": "unknown"},
            {"category_key": []},
            {"incident_type_key": "communication_no_response"},
            {"note": ""},
            {"created_ts": "today"},
        ):
            self.reader.list.return_value = [_record(**{"member_id": 42, **changes})]
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.queries.active_snapshot(member_id=42)

    def test_unordered_and_oversized_results_fail_instead_of_truncating(self):
        self.reader.list.return_value = [_record(1), _record(2)]
        with self.assertRaises(ValueError):
            self.queries.active_snapshot()

        with patch(
            "elbow_helper.features.records.queries.MAX_ACTIVE_LEADERSHIP_RECORDS",
            1,
        ):
            self.reader.list.return_value = [_record(2), _record(1)]
            with self.assertRaises(ValueError):
                self.queries.active_snapshot()

    def test_invalid_member_and_clock_are_rejected_before_or_after_read(self):
        for value in (True, 0, -1, "42"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.queries.active_snapshot(member_id=value)
        self.assertEqual(self.reader.list.call_count, 0)

        queries = LeadershipRecordQueries(self.reader, clock=lambda: "now")
        with self.assertRaises(ValueError):
            queries.active_snapshot()


if __name__ == "__main__":
    unittest.main()
