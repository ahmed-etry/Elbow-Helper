from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from elbow_helper.agent.research.dates import message_time, search_date


class ResearchDateTests(unittest.TestCase):
    def test_bounds_and_request_time_use_utc(self):
        expected = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)
        for value in ("2026-11-01T10:00:00Z", "2026-11-01T10:00:00", "2026-11-01T12:00:00+02:00"):
            with self.subTest(value=value):
                self.assertEqual(search_date(value), expected)
                context = SimpleNamespace(source_message=SimpleNamespace(created_at=datetime.fromisoformat(value)))
                self.assertEqual(message_time(context), expected)
        self.assertIsNone(search_date(None))

    def test_invalid_dates_and_request_times_are_rejected(self):
        for value in (42, {}, "invalid", "2026-13-01"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                search_date(value)
        for value in (None, "2026-11-01", 42):
            context = SimpleNamespace(source_message=SimpleNamespace(created_at=value))
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Invalid request time"):
                message_time(context)
