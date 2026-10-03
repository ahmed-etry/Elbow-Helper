"""Compute saved run times from explicit UTC and IANA timezone rules."""

from __future__ import annotations

from ..actions.contracts import ActionRefused

import calendar
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


WATCHER_MIN_INTERVAL_SECONDS = 15 * 60


def timezone_name(value: str) -> str:
    try:
        ZoneInfo(value)
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError) as error:
        raise ActionRefused("Choose an IANA timezone, such as Europe/Paris.") from error
    return value


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ActionRefused("Use an exact UTC time.") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ActionRefused("Use an exact UTC time.")
    return parsed.astimezone(timezone.utc)


def _clock(value: str) -> tuple[int, int]:
    try:
        hour, minute = map(int, value.split(":"))
    except (AttributeError, TypeError, ValueError) as error:
        raise ActionRefused("Use a time in HH:MM format.") from error
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ActionRefused("Use a time in HH:MM format.")
    return hour, minute


def _local_candidate(day: datetime, *, hour: int, minute: int,
                     zone: ZoneInfo) -> datetime:
    wall_time = day.replace(hour=hour, minute=minute, second=0,
                            microsecond=0, tzinfo=None)
    for offset in range(48 * 60):
        local = (wall_time + timedelta(minutes=offset)).replace(tzinfo=zone, fold=0)
        utc = local.astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == local.replace(tzinfo=None):
            return utc
    raise ValueError("No valid local minute follows that schedule time.")


def next_occurrences(rule: Mapping[str, Any], *, after: datetime,
                     count: int = 3, watcher: bool = False) -> tuple[datetime, ...]:
    """Return the next distinct UTC instants strictly after ``after``."""
    if after.tzinfo is None or count < 1 or count > 10:
        raise ValueError("Use a timezone-aware starting time and one to ten results.")
    after = after.astimezone(timezone.utc)
    kind = rule.get("kind")
    if kind == "once":
        instant = _utc(rule.get("at_utc"))
        return (instant,) if instant > after else ()
    if kind == "interval":
        seconds = rule.get("seconds")
        minimum = WATCHER_MIN_INTERVAL_SECONDS if watcher else 60
        if type(seconds) is not int or seconds < minimum:
            raise ActionRefused(f"Interval must be at least {minimum} seconds.")
        anchor = _utc(rule.get("anchor_utc"))
        elapsed = (after - anchor).total_seconds()
        index = max(0, int(elapsed // seconds) + 1)
        return tuple(anchor + timedelta(seconds=(index + offset) * seconds)
                     for offset in range(count))
    if kind not in ("weekly", "monthly"):
        raise ActionRefused("Choose once, interval, weekly or monthly timing.")
    zone = ZoneInfo(timezone_name(rule.get("timezone")))
    hour, minute = _clock(rule.get("time"))
    days = rule.get("days")
    ceiling = 6 if kind == "weekly" else 31
    floor = 0 if kind == "weekly" else 1
    if (not isinstance(days, list) or not days
            or any(type(day) is not int or not floor <= day <= ceiling for day in days)):
        raise ActionRefused(f"Choose {kind} days from {floor} to {ceiling}.")
    selected = set(days)
    local_start = after.astimezone(zone)
    found: list[datetime] = []
    for offset in range(0, 3660):
        day = local_start + timedelta(days=offset)
        day = day.replace(hour=12, minute=0, second=0, microsecond=0)
        if kind == "weekly" and day.weekday() not in selected:
            continue
        if kind == "monthly" and day.day not in {
            min(selected_day, calendar.monthrange(day.year, day.month)[1])
            for selected_day in selected
        }:
            continue
        instant = _local_candidate(day, hour=hour, minute=minute, zone=zone)
        if instant > after and instant not in found:
            found.append(instant)
            if len(found) == count:
                return tuple(found)
    raise ActionRefused("No future time matches that schedule.")
