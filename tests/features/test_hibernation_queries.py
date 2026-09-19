from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from elbow_helper.features.hibernation.queries import HibernationQueries
from elbow_helper.features.hibernation.state import (
    FALLBACK_INFO_MESSAGE_KEY,
    FALLBACK_THREADS_KEY,
)


class HibernationQueryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)

    def test_snapshot_exposes_status_and_date_without_private_state(self):
        state = {
            "42": {
                "roles": [1, 2], "rank_roles": [3],
                "hibernation_date": "<t:1789617600:F>",
                "private_note": "do not expose",
            },
            "84": {"roles": [4]},
            FALLBACK_THREADS_KEY: {"42": {"thread_id": 123}},
            FALLBACK_INFO_MESSAGE_KEY: 456,
        }
        original = deepcopy(state)
        snapshot = HibernationQueries(
            lambda: state, clock=lambda: self.now,
        ).active_snapshot()

        self.assertEqual([row.member_id for row in snapshot.records], [42, 84])
        self.assertEqual(snapshot.records[0].start_time_status, "recorded")
        self.assertEqual(snapshot.records[1].start_time_status, "unavailable")
        self.assertEqual(snapshot.missing_start_time_count, 1)
        self.assertEqual(snapshot.ignored_metadata_entries, 2)
        self.assertNotIn("roles", repr(snapshot))
        self.assertNotIn("private_note", repr(snapshot))
        self.assertEqual(state, original)

    def test_snapshot_reports_invalid_member_entries_and_enforces_bound(self):
        state = {
            "0": {}, "invalid": {}, "42": "not-an-entry",
            "84": {"hibernation_date": "bad-date"},
        }
        snapshot = HibernationQueries(lambda: state).active_snapshot()
        self.assertEqual(snapshot.stored_member_entry_count, 4)
        self.assertEqual(snapshot.skipped_invalid_member_entries, 3)
        self.assertEqual([row.member_id for row in snapshot.records], [84])
        self.assertEqual(snapshot.missing_start_time_count, 1)

        state = {str(index + 1): {} for index in range(2)}
        with patch(
            "elbow_helper.features.hibernation.queries.MAX_ACTIVE_HIBERNATION_RECORDS", 1,
        ), self.assertRaises(ValueError):
            HibernationQueries(lambda: state).active_snapshot()


if __name__ == "__main__":
    unittest.main()
