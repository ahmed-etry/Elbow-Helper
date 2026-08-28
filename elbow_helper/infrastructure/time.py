"""UTC clock and timezone mechanics without product or Discord policy."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import tzinfo
from datetime import timezone
import re
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

from dateutil import tz as dateutil_tz


UTC = timezone.utc
_FIXED_UTC_OFFSET = re.compile(
    r"^UTC(?P<sign>[+-])(?P<hours>\d{2}):(?P<minutes>\d{2})$"
)


def utc_now() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(UTC)


def resolve_timezone(zone_name: str) -> tzinfo | None:
    """Resolve an IANA timezone or canonical fixed UTC offset."""
    fixed = _fixed_offset_timezone(zone_name)
    if fixed is not None:
        return fixed
    if zone_name.startswith(("UTC+", "UTC-")):
        return None
    try:
        return ZoneInfo(zone_name)
    except ZoneInfoNotFoundError:
        return dateutil_tz.gettz(zone_name)


def format_utc_offset(
    zone_name: str,
    at: datetime | None = None,
) -> str | None:
    """Return a timezone's offset at one instant as ``UTC+HH:MM``."""
    zone = resolve_timezone(zone_name)
    if zone is None:
        return None
    reference = at or utc_now()
    offset = reference.astimezone(zone).utcoffset()
    if offset is None:
        return None

    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def fixed_utc_offset_name(
    zone_name: str,
    at: datetime | None = None,
) -> str | None:
    """Snapshot a timezone's offset as a canonical fixed-zone name."""
    offset_name = format_utc_offset(zone_name, at)
    if offset_name == "UTC+00:00":
        return "UTC"
    return offset_name


def _fixed_offset_timezone(zone_name: str) -> tzinfo | None:
    if zone_name == "UTC":
        return UTC
    match = _FIXED_UTC_OFFSET.fullmatch(zone_name)
    if match is None:
        return None

    hours = int(match.group("hours"))
    minutes = int(match.group("minutes"))
    if hours > 23 or minutes > 59:
        return None

    offset = timedelta(hours=hours, minutes=minutes)
    if match.group("sign") == "-":
        offset = -offset
    try:
        return timezone(offset, name=zone_name)
    except ValueError:
        return None
