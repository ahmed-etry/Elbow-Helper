from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from elbow_helper.features.event_stats.channels import EventStatsChannelsMixin
from elbow_helper.features.event_stats.queries import EventStatsQueries
from elbow_helper.features.event_stats.timeutils import (
    cwl_window, trader_refresh_point,
)


class EventStatsQueryTests(unittest.TestCase):
    def setUp(self):
        self.observed = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
        self.owner = EventStatsChannelsMixin()
        self.events = (
            {
                "key": "members", "name": "Members", "source": "preset",
                "type": "counter", "enabled": True, "position": 0,
                "roles_to_count": [10, 20], "grace_period_hours": 0,
            },
            {
                "key": "cwl", "name": "CWL", "source": "preset",
                "type": "recurring", "enabled": True, "position": 1,
                "schedule_shape": "range", "range_fn": cwl_window,
                "grace_period_hours": 24,
            },
            {
                "key": "trader_refresh", "name": "Trader", "source": "preset",
                "type": "recurring", "enabled": True, "position": 2,
                "schedule_shape": "point", "point_fn": trader_refresh_point,
                "grace_period_hours": 0,
            },
            {
                "key": "event_custom", "name": "Custom", "source": "custom",
                "type": "one-time", "enabled": False, "position": 3,
                "start": self.observed + timedelta(days=1),
                "end": self.observed + timedelta(days=2),
                "timezone": "Europe/Paris", "grace_period_hours": 6,
            },
        )
        member_a = SimpleNamespace(id=1)
        member_b = SimpleNamespace(id=2)
        role = SimpleNamespace(id=10, members=[member_a, member_b])
        self.guild = SimpleNamespace(
            get_role=lambda role_id: role if role_id == 10 else None,
        )
        self.queries = EventStatsQueries(
            lambda: self.events,
            event_phase=self.owner._event_phase,
            recurring_range=self.owner._recurring_range,
            recurring_point=self.owner._recurring_point,
            clock=lambda: self.observed,
        )

    def test_snapshot_reuses_owner_schedule_and_phase_calculations(self):
        snapshot = self.queries.snapshot(self.guild)
        rows = {row.event_key: row for row in snapshot.rows}

        self.assertEqual(snapshot.observed_at, self.observed.isoformat())
        self.assertEqual(rows["members"].member_count, 2)
        self.assertEqual(rows["members"].count_coverage, "partial_missing_roles")
        self.assertEqual(rows["members"].missing_role_count, 1)
        expected_start, expected_end = cwl_window(self.observed)
        self.assertEqual(rows["cwl"].start_at, expected_start.isoformat())
        self.assertEqual(rows["cwl"].end_at, expected_end.isoformat())
        self.assertEqual(
            rows["cwl"].phase,
            self.owner._event_phase(self.events[1], self.observed),
        )
        self.assertEqual(
            rows["trader_refresh"].next_at,
            trader_refresh_point(self.observed).isoformat(),
        )
        self.assertEqual(rows["event_custom"].phase, "disabled")
        self.assertEqual(rows["event_custom"].timezone, "Europe/Paris")

    def test_counter_deduplicates_members_across_configured_roles(self):
        member = SimpleNamespace(id=1)
        roles = {
            10: SimpleNamespace(id=10, members=[member]),
            20: SimpleNamespace(id=20, members=[member]),
        }
        guild = SimpleNamespace(get_role=roles.get)

        row = self.queries.snapshot(guild).rows[0]

        self.assertEqual(row.member_count, 1)
        self.assertEqual(row.count_coverage, "complete")
        self.assertEqual(row.missing_role_count, 0)

    def test_invalid_event_fails_complete_snapshot(self):
        invalid = ({"key": "bad", "type": "unknown"},)
        queries = EventStatsQueries(
            lambda: invalid,
            event_phase=self.owner._event_phase,
            recurring_range=self.owner._recurring_range,
            recurring_point=self.owner._recurring_point,
            clock=lambda: self.observed,
        )

        with self.assertRaises(ValueError):
            queries.snapshot(self.guild)


if __name__ == "__main__":
    unittest.main()
