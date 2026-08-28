"""Time helpers and recurring event windows for event stats."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import tzinfo as Timezone
from typing import Tuple

from elbow_helper.infrastructure.time import UTC


def fmt_dh(delta: timedelta, *, minute_step: int = 1) -> str:
    total_seconds = max(0, int(delta.total_seconds()))
    if total_seconds < 24 * 3600:
        if minute_step > 1 and total_seconds <= minute_step * 60:
            return "Almost Over"
        total_minutes = max(1, (total_seconds + 59) // 60)
        if minute_step > 1:
            stepped_minutes = ((total_minutes + minute_step - 1) // minute_step) * minute_step
            total_minutes = stepped_minutes
        hours, minutes = divmod(total_minutes, 60)
        if hours > 0 and minutes > 0:
            return f"{hours}H {minutes}M"
        if hours > 0:
            return f"{hours}H"
        return f"{minutes}M"

    total_hours = total_seconds // 3600
    days = total_hours // 24
    hours = total_hours % 24
    return f"{days}D {hours}H" if days > 0 else f"{hours}H"


def parse_event_datetime_input(
    raw_value: str,
    tzinfo: Timezone,
) -> datetime | None:
    cleaned = (raw_value or "").strip()
    if not cleaned:
        return None

    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        parsed = None

    if parsed is not None:
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=tzinfo)
        return parsed.astimezone(tzinfo)

    normalized = cleaned.replace("T", " ")
    formats = (
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d %I:%M %p",
        "%Y/%m/%d %I:%M %p",
    )
    for fmt in formats:
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=tzinfo)
        except ValueError:
            continue
    return None


def format_event_datetime_local(dt: datetime, tzinfo: Timezone) -> str:
    return dt.astimezone(tzinfo).strftime("%Y-%m-%d %H:%M")


def cwl_window(reference: datetime) -> Tuple[datetime, datetime]:
    start = reference.replace(day=1, hour=8, minute=0, second=0, microsecond=0, tzinfo=UTC)
    end = start.replace(day=9)
    if reference >= end:
        start = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
        end = start.replace(day=9)
    return start, end


def clangames_window(reference: datetime) -> Tuple[datetime, datetime]:
    start = reference.replace(day=22, hour=8, minute=0, second=0, microsecond=0, tzinfo=UTC)
    end = reference.replace(day=28, hour=8, minute=0, second=0, microsecond=0, tzinfo=UTC)
    if reference > end:
        if start.month == 12:
            start = start.replace(year=start.year + 1, month=1)
            end = end.replace(year=end.year + 1, month=1)
        else:
            start = start.replace(month=start.month + 1)
            end = end.replace(month=end.month + 1)
    return start, end


def league_reset_point(reference: datetime) -> datetime:
    reference_utc = reference.astimezone(UTC) if reference.tzinfo is not None else reference.replace(tzinfo=UTC)
    anchor = datetime(2025, 11, 3, 5, 0, 0, tzinfo=UTC)
    cycle = timedelta(days=28)

    if reference_utc < anchor:
        return anchor

    cycles_elapsed = (reference_utc - anchor) // cycle
    next_point = anchor + (cycle * cycles_elapsed)
    if reference_utc > next_point:
        next_point += cycle
    return next_point


def season_end_point(reference: datetime) -> datetime:
    reference_utc = reference.astimezone(UTC) if reference.tzinfo is not None else reference.replace(tzinfo=UTC)
    candidate = reference_utc.replace(day=1, hour=8, minute=0, second=0, microsecond=0)
    if reference_utc < candidate:
        return candidate
    if candidate.month == 12:
        return candidate.replace(year=candidate.year + 1, month=1)
    return candidate.replace(month=candidate.month + 1)


def trader_refresh_point(reference: datetime) -> datetime:
    reference_utc = reference.astimezone(UTC) if reference.tzinfo is not None else reference.replace(tzinfo=UTC)
    days_until_tuesday = (1 - reference_utc.weekday()) % 7
    candidate = (reference_utc + timedelta(days=days_until_tuesday)).replace(hour=8, minute=0, second=0, microsecond=0)
    if reference_utc >= candidate:
        candidate += timedelta(days=7)
    return candidate


def raid_weekend_window(reference: datetime) -> Tuple[datetime, datetime]:
    days_back_to_friday = (reference.weekday() - 4) % 7
    start = (reference - timedelta(days=days_back_to_friday)).replace(hour=7, minute=0, second=0, microsecond=0, tzinfo=UTC)
    end = start + timedelta(days=3)
    if reference < start:
        start -= timedelta(days=7)
        end -= timedelta(days=7)
    if reference >= end:
        start += timedelta(days=7)
        end += timedelta(days=7)
    return start, end
