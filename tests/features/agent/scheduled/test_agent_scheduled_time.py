"""Saved schedules use exact UTC instants and local calendar rules."""

from datetime import datetime, timezone
import unittest

from elbow_helper.features.agent.scheduled.time_rules import next_occurrences


class ScheduledTimeTests(unittest.TestCase):
    def test_one_off_and_interval_use_utc(self):
        start = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        self.assertEqual(next_occurrences(
            {"kind": "once", "at_utc": "2026-01-01T13:00:00Z"}, after=start),
            (datetime(2026, 1, 1, 13, tzinfo=timezone.utc),),
        )
        instants = next_occurrences({
            "kind": "interval", "anchor_utc": "2026-01-01T12:00:00Z",
            "seconds": 3600,
        }, after=start)
        self.assertEqual([item.hour for item in instants], [13, 14, 15])

    def test_weekly_local_time_tracks_dst(self):
        rule = {"kind": "weekly", "days": [0], "time": "09:00",
                "timezone": "Europe/Paris"}
        instants = next_occurrences(rule,
            after=datetime(2026, 3, 23, 8, tzinfo=timezone.utc), count=2)
        self.assertEqual(instants, (
            datetime(2026, 3, 30, 7, tzinfo=timezone.utc),
            datetime(2026, 4, 6, 7, tzinfo=timezone.utc),
        ))

    def test_nonexistent_local_time_runs_at_first_valid_minute(self):
        rule = {"kind": "weekly", "days": [6], "time": "02:30",
                "timezone": "Europe/Paris"}
        result = next_occurrences(rule,
            after=datetime(2026, 3, 28, 0, tzinfo=timezone.utc), count=1)
        self.assertEqual(result, (datetime(2026, 3, 29, 1, tzinfo=timezone.utc),))

    def test_monthly_missing_day_runs_on_last_day(self):
        rule = {"kind": "monthly", "days": [31], "time": "10:00",
                "timezone": "Europe/Paris"}
        result = next_occurrences(rule,
            after=datetime(2026, 1, 31, 12, tzinfo=timezone.utc), count=1)
        self.assertEqual(result, (datetime(2026, 2, 28, 9, tzinfo=timezone.utc),))

    def test_monthly_short_days_merge_and_resume_next_month(self):
        rule = {"kind": "monthly", "days": [30, 31], "time": "10:00",
                "timezone": "Europe/Paris"}
        result = next_occurrences(rule,
            after=datetime(2026, 2, 1, tzinfo=timezone.utc), count=3)
        self.assertEqual(result, (
            datetime(2026, 2, 28, 9, tzinfo=timezone.utc),
            datetime(2026, 3, 30, 8, tzinfo=timezone.utc),
            datetime(2026, 3, 31, 8, tzinfo=timezone.utc),
        ))

    def test_clock_jump_does_not_skip_next_week(self):
        rule = {"kind": "weekly", "days": [6], "time": "02:30",
                "timezone": "Europe/Paris"}
        result = next_occurrences(rule,
            after=datetime(2026, 3, 28, tzinfo=timezone.utc), count=2)
        self.assertEqual(result, (
            datetime(2026, 3, 29, 1, tzinfo=timezone.utc),
            datetime(2026, 4, 5, 0, 30, tzinfo=timezone.utc),
        ))

    def test_watcher_interval_has_fifteen_minute_floor(self):
        with self.assertRaisesRegex(ValueError, "900 seconds"):
            next_occurrences({"kind": "interval", "seconds": 899,
                              "anchor_utc": "2026-01-01T00:00:00Z"},
                             after=datetime(2026, 1, 1, tzinfo=timezone.utc),
                             watcher=True)

