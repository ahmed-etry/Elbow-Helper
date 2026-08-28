"""Monthly roster schedule calculations using IANA timezones."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime
from datetime import tzinfo
from datetime import timezone

from dateutil.tz import datetime_exists
from dateutil.tz import resolve_imaginary

from elbow_helper.infrastructure.time import resolve_timezone


@dataclass(frozen=True)
class ScheduleWindow:
    cycle_key: str
    opens_at: datetime
    closes_at: datetime


def parse_clock(value: str) -> tuple[int, int] | None:
    try:
        hour_text, minute_text = value.strip().split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (AttributeError, TypeError, ValueError):
        return None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour, minute


def normalize_clock(value: str) -> str | None:
    parsed = parse_clock(value)
    return f"{parsed[0]:02d}:{parsed[1]:02d}" if parsed else None


def parse_day_rule(value: str | int) -> str | None:
    """Return a canonical fixed-day or month-end-relative rule."""
    normalized = str(value).strip().lower()
    if normalized == "last":
        return "last"
    if normalized.startswith("last-"):
        try:
            offset = int(normalized.removeprefix("last-"))
        except ValueError:
            return None
        return f"last-{offset}" if 1 <= offset <= 2 else None
    try:
        day = int(normalized)
    except (TypeError, ValueError):
        return None
    return str(day) if 1 <= day <= 28 else None


def one_off_window(
    *,
    opens_on: str,
    closes_on: str,
    timezone_name: str,
) -> ScheduleWindow | None:
    tzinfo = resolve_timezone(timezone_name)
    if tzinfo is None:
        return None
    try:
        opens_at = _wall_datetime(
            datetime.strptime(opens_on.strip(), "%Y-%m-%d %H:%M"),
            tzinfo,
        )
        closes_at = _wall_datetime(
            datetime.strptime(closes_on.strip(), "%Y-%m-%d %H:%M"),
            tzinfo,
        )
    except (AttributeError, TypeError, ValueError):
        return None
    opens_utc = opens_at.astimezone(timezone.utc)
    closes_utc = closes_at.astimezone(timezone.utc)
    if closes_utc <= opens_utc:
        return None
    return ScheduleWindow(
        cycle_key=f"once:{int(opens_utc.timestamp())}",
        opens_at=opens_utc,
        closes_at=closes_utc,
    )


def _month_after(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + month - 1 + delta
    shifted_year, shifted_month = divmod(index, 12)
    return shifted_year, shifted_month + 1


def _wall_datetime(naive: datetime, zone: tzinfo) -> datetime:
    """Attach a timezone, moving nonexistent DST times forward by the DST gap."""
    aware = naive.replace(tzinfo=zone)
    return aware if datetime_exists(aware) else resolve_imaginary(aware)


def _resolve_day(year: int, month: int, rule: str) -> int:
    final_day = calendar.monthrange(year, month)[1]
    if rule == "last":
        return final_day
    if rule.startswith("last-"):
        return final_day - int(rule.removeprefix("last-"))
    return int(rule)


def schedule_window(
    *,
    year: int,
    month: int,
    timezone_name: str,
    open_day: str | int,
    open_time: str,
    close_day: str,
    close_time: str,
) -> ScheduleWindow | None:
    """Build the window anchored to an opening month in the selected timezone."""
    tzinfo = resolve_timezone(timezone_name)
    open_clock = parse_clock(open_time)
    close_clock = parse_clock(close_time)
    normalized_open = parse_day_rule(open_day)
    normalized_close = parse_day_rule(close_day)
    if (
        tzinfo is None
        or open_clock is None
        or close_clock is None
        or normalized_open is None
        or normalized_close is None
    ):
        return None

    opens_at = _wall_datetime(
        datetime(year, month, _resolve_day(year, month, normalized_open), *open_clock),
        tzinfo,
    )
    close_year, close_month = year, month
    closes_at = _wall_datetime(
        datetime(
            close_year,
            close_month,
            _resolve_day(close_year, close_month, normalized_close),
            *close_clock,
        ),
        tzinfo,
    )
    if closes_at <= opens_at:
        close_year, close_month = _month_after(year, month)
        closes_at = _wall_datetime(
            datetime(
                close_year,
                close_month,
                _resolve_day(close_year, close_month, normalized_close),
                *close_clock,
            ),
            tzinfo,
        )
    return ScheduleWindow(
        cycle_key=f"{year:04d}-{month:02d}",
        opens_at=opens_at.astimezone(timezone.utc),
        closes_at=closes_at.astimezone(timezone.utc),
    )


def due_window(roster: object, now: datetime | None = None) -> ScheduleWindow | None:
    now_utc = now or datetime.now(timezone.utc)
    timezone_name = str(getattr(roster, "schedule_utc_offset", "") or "")
    tzinfo = resolve_timezone(timezone_name)
    if tzinfo is None:
        return None
    local_now = now_utc.astimezone(tzinfo)
    candidates: list[ScheduleWindow] = []
    for delta in (-1, 0):
        year, month = _shift_month(local_now.year, local_now.month, delta)
        window = schedule_window(
            year=year,
            month=month,
            timezone_name=timezone_name,
            open_day=str(getattr(roster, "open_day", "") or ""),
            open_time=str(getattr(roster, "open_time", "") or ""),
            close_day=str(getattr(roster, "close_day", "") or ""),
            close_time=str(getattr(roster, "close_time", "") or ""),
        )
        if window:
            candidates.append(window)
    active = [window for window in candidates if window.opens_at <= now_utc]
    return max(active, key=lambda item: item.opens_at) if active else None


def next_window(roster: object, now: datetime | None = None) -> ScheduleWindow | None:
    now_utc = now or datetime.now(timezone.utc)
    timezone_name = str(getattr(roster, "schedule_utc_offset", "") or "")
    tzinfo = resolve_timezone(timezone_name)
    if tzinfo is None:
        return None
    local_now = now_utc.astimezone(tzinfo)
    candidates = []
    for delta in (0, 1):
        year, month = _shift_month(local_now.year, local_now.month, delta)
        window = schedule_window(
            year=year,
            month=month,
            timezone_name=timezone_name,
            open_day=str(getattr(roster, "open_day", "") or ""),
            open_time=str(getattr(roster, "open_time", "") or ""),
            close_day=str(getattr(roster, "close_day", "") or ""),
            close_time=str(getattr(roster, "close_time", "") or ""),
        )
        if window and window.opens_at > now_utc:
            candidates.append(window)
    return min(candidates, key=lambda item: item.opens_at) if candidates else None
