"""Preview values retain zero and distinguish missing values from visibility."""

from datetime import datetime, timedelta, timezone
import unittest

from elbow_helper.features.agent.actions.values import display_value


class PreviewValueTests(unittest.TestCase):
    def test_missing_zero_lists_and_boolean_states_remain_distinct(self):
        for value in (None, "", [], ()):
            with self.subTest(value=value):
                self.assertEqual(display_value(value), "Not set")
        self.assertEqual(display_value(0), "0")
        self.assertEqual(display_value([11, 12]), "11, 12")
        self.assertEqual(display_value(False), "No")
        self.assertEqual(display_value(True), "Yes")
        self.assertEqual(display_value(False, visibility=True), "Hidden")
        self.assertEqual(display_value(True, visibility=True), "Visible")

    def test_same_instant_has_the_same_discord_timestamp_in_every_zone(self):
        instant = datetime(2026, 1, 1, tzinfo=timezone.utc)
        expected = f"<t:{int(instant.timestamp())}:f>"
        for offset in (-12, 0, 5, 14):
            with self.subTest(offset=offset):
                local = instant.astimezone(timezone(timedelta(hours=offset)))
                self.assertEqual(display_value(local), expected)
