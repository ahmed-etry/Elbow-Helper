from datetime import datetime, timezone
import unittest

from elbow_helper.features.member_lifecycle.queries import MemberLifecycleQueries


class MemberLifecycleQueryTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "members": {
                "1": {
                    "platform": "Reddit",
                    "joined_at_iso": "2026-09-18T10:00:00+00:00",
                    "left": False,
                },
                "2": {
                    "platform": "Discord",
                    "joined_at_iso": "2026-09-17T10:00:00+00:00",
                    "left": False,
                },
                "bad": {"platform": "Reddit"},
            },
            "last_seen": {
                "1": {"ts_iso": "2026-09-19T09:00:00+00:00", "channel_id": 10},
                "2": {"ts_iso": "2026-09-19T08:00:00+00:00", "channel_id": 20},
            },
            "platform_counts": {"Reddit": 2, "Discord": 1, "bad": -1},
            "overdue_applicant_ids": [2, "broken"],
            "last_weekly_report_iso": "2026-09-15T12:00:00+00:00",
            "last_applicant_scan_iso": "2026-09-19T06:00:00+00:00",
        }
        self.queries = MemberLifecycleQueries(
            lambda: self.state,
            clock=lambda: datetime(2026, 9, 19, 12, tzinfo=timezone.utc),
        )

    def test_snapshot_is_bounded_honest_and_activity_permission_filtered(self):
        registrations = self.queries.activity_registrations(
            current_member_ids={1, 2, 3},
        )
        self.assertEqual(
            [(row.member_id, row.channel_id) for row in registrations],
            [(1, 10), (2, 20)],
        )

        snapshot = self.queries.snapshot(
            current_members={1: "One", 2: "Two", 3: "Three"},
            activity_channels={1: 10},
        )

        self.assertEqual(snapshot.observed_at, "2026-09-19T12:00:00+00:00")
        self.assertEqual(snapshot.stored_member_entry_count, 3)
        self.assertEqual(snapshot.skipped_invalid_member_entries, 1)
        self.assertEqual(snapshot.untracked_current_member_count, 1)
        self.assertEqual(snapshot.skipped_invalid_platform_counts, 1)
        self.assertEqual(snapshot.skipped_invalid_overdue_entries, 1)
        self.assertEqual(snapshot.platform_counts, (("Reddit", 2), ("Discord", 1)))
        self.assertEqual(snapshot.overdue_applicants[0].member_id, 2)
        self.assertEqual([row.member_id for row in snapshot.rows], [1, 2])
        self.assertEqual(snapshot.rows[0].last_seen_channel_id, 10)
        self.assertIsNone(snapshot.rows[1].last_seen_at)
        self.assertTrue(snapshot.rows[1].overdue_applicant)

    def test_changed_activity_channel_is_not_exposed(self):
        snapshot = self.queries.snapshot(
            current_members={1: "One"}, activity_channels={1: 99},
        )
        self.assertIsNone(snapshot.rows[0].last_seen_channel_id)

    def test_oversize_state_is_rejected(self):
        self.state["platform_counts"] = {
            str(index): index for index in range(101)
        }
        with self.assertRaises(ValueError):
            self.queries.snapshot(
                current_members={1: "One"}, activity_channels={},
            )


if __name__ == "__main__":
    unittest.main()
