"""Supported community timezones and their product-facing text forms."""

from __future__ import annotations

from datetime import datetime
from datetime import tzinfo

from elbow_helper.infrastructure.time import format_utc_offset
from elbow_helper.infrastructure.time import resolve_timezone


TIMEZONE_ENTRIES = (
    ("UTC", "UTC"),
    # Europe
    ("London", "Europe/London"),
    ("Paris", "Europe/Paris"),
    ("Berlin", "Europe/Berlin"),
    ("Amsterdam", "Europe/Amsterdam"),
    ("Brussels", "Europe/Brussels"),
    ("Madrid", "Europe/Madrid"),
    ("Rome", "Europe/Rome"),
    ("Warsaw", "Europe/Warsaw"),
    # MENA / Africa
    ("Beirut", "Asia/Beirut"),
    ("Cairo", "Africa/Cairo"),
    ("Dubai", "Asia/Dubai"),
    ("Riyadh", "Asia/Riyadh"),
    ("Johannesburg", "Africa/Johannesburg"),
    # South / Southeast Asia
    ("Karachi", "Asia/Karachi"),
    ("Kolkata", "Asia/Kolkata"),
    ("Dhaka", "Asia/Dhaka"),
    ("Singapore", "Asia/Singapore"),
    ("Manila", "Asia/Manila"),
    # North America
    ("New York", "America/New_York"),
    ("Toronto", "America/Toronto"),
    ("Detroit", "America/Detroit"),
    ("Chicago", "America/Chicago"),
    ("Denver", "America/Denver"),
    ("Phoenix", "America/Phoenix"),
    ("Los Angeles", "America/Los_Angeles"),
    ("Vancouver", "America/Vancouver"),
    # Oceania
    ("Sydney", "Australia/Sydney"),
    ("Melbourne", "Australia/Melbourne"),
    ("Darwin", "Australia/Darwin"),
)


def format_timezone_display(
    zone_name: str,
    at: datetime | None = None,
) -> str:
    """Return the supported timezone's current offset and community label."""
    for label, candidate in TIMEZONE_ENTRIES:
        if candidate == zone_name:
            offset = format_utc_offset(zone_name, at) or "UTC+00:00"
            return f"{offset} - {label}"
    return zone_name


def canonical_timezone_name(user_input: str) -> str | None:
    """Resolve accepted input into an IANA name or fixed UTC offset."""
    candidate = (user_input or "").strip()
    if not candidate:
        return None

    if resolve_timezone(candidate) is not None:
        return candidate

    lowered = candidate.lower()
    for label, zone_name in TIMEZONE_ENTRIES:
        if lowered == label.lower():
            return zone_name
        if lowered == format_timezone_display(zone_name).lower():
            return zone_name
        if lowered.endswith(f"- {label.lower()}"):
            return zone_name
    return None


def resolve_timezone_input(user_input: str) -> tzinfo | None:
    """Resolve accepted community input into a timezone object."""
    canonical = canonical_timezone_name(user_input)
    if canonical is None:
        return None
    return resolve_timezone(canonical)
