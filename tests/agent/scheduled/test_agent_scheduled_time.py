"""Saved schedules use exact UTC instants and local calendar rules."""

from datetime import datetime, timezone
import unittest

from elbow_helper.agent.scheduled.time_rules import next_occurrences


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

    def test_calendar_boundaries_keep_exact_utc_occurrences(self):
        cases = (
            ({'kind': 'weekly', 'days': [0], 'time': '09:00'}, '2026-03-23T08:00',
             ('2026-03-30T07:00', '2026-04-06T07:00')),
            ({'kind': 'weekly', 'days': [6], 'time': '02:30'}, '2026-03-28T00:00',
             ('2026-03-29T01:00', '2026-04-05T00:30')),
            ({'kind': 'monthly', 'days': [31], 'time': '10:00'}, '2026-01-31T12:00',
             ('2026-02-28T09:00',)),
            ({'kind': 'monthly', 'days': [30, 31], 'time': '10:00'}, '2026-02-01T00:00',
             ('2026-02-28T09:00', '2026-03-30T08:00', '2026-03-31T08:00')),
        )
        def utc(value):
            return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        for rule, after, expected in cases:
            with self.subTest(rule=rule, after=after):
                self.assertEqual(next_occurrences({**rule, 'timezone': 'Europe/Paris'},
                    after=utc(after), count=len(expected)), tuple(map(utc, expected)))


    def test_watcher_interval_has_fifteen_minute_floor(self):
        with self.assertRaisesRegex(ValueError, "900 seconds"):
            next_occurrences({"kind": "interval", "seconds": 899,
                              "anchor_utc": "2026-01-01T00:00:00Z"},
                             after=datetime(2026, 1, 1, tzinfo=timezone.utc),
                             watcher=True)
