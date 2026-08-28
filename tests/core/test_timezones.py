from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import unittest

from elbow_helper.discord.timezones import build_timezone_choices
from elbow_helper.discord.timezones import build_timezone_select_options
from elbow_helper.domain.timezones import TIMEZONE_ENTRIES
from elbow_helper.domain.timezones import canonical_timezone_name
from elbow_helper.domain.timezones import format_timezone_display
from elbow_helper.domain.timezones import resolve_timezone_input
from elbow_helper.infrastructure.time import UTC
from elbow_helper.infrastructure.time import fixed_utc_offset_name
from elbow_helper.infrastructure.time import format_utc_offset
from elbow_helper.infrastructure.time import resolve_timezone
from elbow_helper.infrastructure.time import utc_now


class TimeInfrastructureTests(unittest.TestCase):
    def test_utc_now_returns_an_aware_utc_datetime(self) -> None:
        current = utc_now()

        self.assertIs(current.tzinfo, UTC)
        self.assertEqual(current.utcoffset(), timedelta(0))

    def test_iana_timezone_tracks_daylight_saving_at_the_given_instant(self) -> None:
        paris = resolve_timezone("Europe/Paris")
        self.assertIsNotNone(paris)

        winter = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        summer = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

        self.assertEqual(winter.astimezone(paris).utcoffset(), timedelta(hours=1))
        self.assertEqual(summer.astimezone(paris).utcoffset(), timedelta(hours=2))

    def test_fixed_offsets_resolve_without_daylight_saving(self) -> None:
        fixed = resolve_timezone("UTC-03:30")
        self.assertIsNotNone(fixed)

        self.assertEqual(
            datetime(2026, 1, 1, tzinfo=fixed).utcoffset(),
            -timedelta(hours=3, minutes=30),
        )
        self.assertEqual(
            datetime(2026, 7, 1, tzinfo=fixed).utcoffset(),
            -timedelta(hours=3, minutes=30),
        )

    def test_invalid_fixed_offsets_and_unknown_zones_are_rejected(self) -> None:
        self.assertIsNone(resolve_timezone("UTC+24:00"))
        self.assertIsNone(resolve_timezone("UTC+02:60"))
        self.assertIsNone(resolve_timezone("Not/A_Real_Zone"))

    def test_offset_formatting_and_fixed_snapshot_preserve_existing_forms(self) -> None:
        summer = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

        self.assertEqual(
            format_utc_offset("Europe/Paris", summer),
            "UTC+02:00",
        )
        self.assertEqual(
            fixed_utc_offset_name("Europe/Paris", summer),
            "UTC+02:00",
        )
        self.assertEqual(format_utc_offset("UTC", summer), "UTC+00:00")
        self.assertEqual(fixed_utc_offset_name("UTC", summer), "UTC")
        self.assertIsNone(format_utc_offset("Not/A_Real_Zone", summer))


class CommunityTimezoneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.summer = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)

    def test_supported_timezone_catalogue_is_preserved(self) -> None:
        self.assertEqual(len(TIMEZONE_ENTRIES), 30)
        self.assertEqual(TIMEZONE_ENTRIES[0], ("UTC", "UTC"))
        self.assertEqual(TIMEZONE_ENTRIES[-1], ("Darwin", "Australia/Darwin"))

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

    def test_resolved_community_input_returns_the_expected_zone(self) -> None:
        resolved = resolve_timezone_input("Beirut")
        self.assertIsNotNone(resolved)

        local = self.summer.astimezone(resolved)
        self.assertEqual(local.hour, 15)
        self.assertEqual(local.utcoffset(), timedelta(hours=3))

    def test_discord_select_options_preserve_labels_values_and_descriptions(
        self,
    ) -> None:
        options = build_timezone_select_options(self.summer)

        self.assertEqual(len(options), 30)
        self.assertEqual(
            (options[0].label, options[0].value, options[0].description),
            ("UTC+00:00 - UTC", "UTC", "UTC"),
        )
        self.assertEqual(
            (options[2].label, options[2].value, options[2].description),
            ("UTC+02:00 - Paris", "Europe/Paris", "Europe/Paris"),
        )

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
