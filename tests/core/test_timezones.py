from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import unittest

from elbow_helper.infrastructure.discord.timezones import build_timezone_choices
from elbow_helper.domain.timezones import canonical_timezone_name
from elbow_helper.domain.timezones import format_timezone_display
from elbow_helper.infrastructure.time import UTC
from elbow_helper.infrastructure.time import resolve_timezone


class TimeInfrastructureTests(unittest.TestCase):

    def test_timezone_resolution_at_dst_fixed_and_invalid_boundaries(self):
        for name, month, offset in (
            ('Europe/Paris', 1, 60), ('Europe/Paris', 7, 120),
            ('UTC-03:30', 1, -210), ('UTC-03:30', 7, -210),
            ('UTC+24:00', 1, None), ('UTC+02:60', 1, None), ('Not/A_Real_Zone', 1, None),
        ):
            with self.subTest(name=name, month=month):
                zone = resolve_timezone(name)
                if offset is None:
                    self.assertIsNone(zone)
                else:
                    self.assertIsNotNone(zone)
                    self.assertEqual(datetime(2026, month, 15, tzinfo=UTC).astimezone(zone).utcoffset(),
                                     timedelta(minutes=offset))


class CommunityTimezoneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.summer = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


    def test_display_uses_the_offset_at_the_requested_instant(self) -> None:
        winter = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

        self.assertEqual(
            format_timezone_display("Europe/Paris", self.summer),
            "UTC+02:00 - Paris",
        )
        self.assertEqual(
            format_timezone_display("Europe/Paris", winter),
            "UTC+01:00 - Paris",
        )
        self.assertEqual(
            format_timezone_display("UTC+03:00", self.summer),
            "UTC+03:00",
        )

    def test_canonical_input_accepts_zone_label_display_and_fixed_offset(self) -> None:
        self.assertEqual(canonical_timezone_name("Paris"), "Europe/Paris")
        self.assertEqual(
            canonical_timezone_name("Europe/Paris"),
            "Europe/Paris",
        )
        self.assertEqual(
            canonical_timezone_name("UTC+02:00 - Paris"),
            "Europe/Paris",
        )
        self.assertEqual(canonical_timezone_name("UTC+03:00"), "UTC+03:00")
        self.assertIsNone(canonical_timezone_name(""))
        self.assertIsNone(canonical_timezone_name("not a timezone"))


    def test_discord_autocomplete_preserves_filter_order_limit_and_fallback(
        self,
    ) -> None:
        paris = build_timezone_choices("par", self.summer)
        all_choices = build_timezone_choices("", self.summer)
        fallback = build_timezone_choices("not-a-zone", self.summer)

        self.assertEqual(
            [(choice.name, choice.value) for choice in paris],
            [("14:00 - Paris", "Europe/Paris")],
        )
        self.assertEqual(len(all_choices), 25)
        self.assertEqual(
            (all_choices[0].name, all_choices[0].value),
            ("05:00 - Los Angeles", "America/Los_Angeles"),
        )
        self.assertEqual(
            [(choice.name, choice.value) for choice in fallback],
            [("12:00 - UTC", "UTC")],
        )
