"""Compute saved run times from explicit UTC and IANA timezone rules."""

from __future__ import annotations

from ..actions.contracts import ActionRefused
from ..wording import (
    ACTION_SCHEDULE_END_AFTER_FIRST, ACTION_SCHEDULE_END_CHOICE,
    ACTION_SCHEDULE_RUN_COUNT, ACTION_SCHEDULE_ONCE_END,
    ACTION_SCHEDULE_DESTINATION_LIMIT,
)

import calendar
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


WATCHER_MIN_INTERVAL_SECONDS = 15 * 60
MIN_MESSAGE_INTERVAL_SECONDS = 60


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


def _unbounded_occurrences(rule: Mapping[str, Any], *, after: datetime,
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
        minimum = WATCHER_MIN_INTERVAL_SECONDS if watcher else MIN_MESSAGE_INTERVAL_SECONDS
        if type(seconds) is not int or seconds < minimum:
            if not watcher:
                raise ActionRefused(ACTION_SCHEDULE_DESTINATION_LIMIT)
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


def validate_end(rule: Mapping[str, Any], first: datetime) -> None:
    if "end_utc" in rule and "runs" in rule:
        raise ActionRefused(ACTION_SCHEDULE_END_CHOICE)
    if rule.get("kind") == "once" and ("end_utc" in rule or "runs" in rule):
        raise ActionRefused(ACTION_SCHEDULE_ONCE_END)
    if "runs" in rule and (type(rule["runs"]) is not int or rule["runs"] < 1):
        raise ActionRefused(ACTION_SCHEDULE_RUN_COUNT)
    if "end_utc" in rule and _utc(rule["end_utc"]) <= first:
        raise ActionRefused(ACTION_SCHEDULE_END_AFTER_FIRST)


def schedule_expired(rule: Mapping[str, Any], now: datetime, runs_completed: int = 0) -> bool:
    return ("end_utc" in rule and now > _utc(rule["end_utc"])) or (
        "runs" in rule and runs_completed >= rule["runs"]
    )


def next_occurrences(rule: Mapping[str, Any], *, after: datetime, count: int = 3,
                     watcher: bool = False, runs_completed: int = 0) -> tuple[datetime, ...]:
    if after.tzinfo is None or not 1 <= count <= 10:
        raise ValueError("Use a timezone-aware starting time and one to ten results.")
    if schedule_expired(rule, after, runs_completed):
        return ()
    if "runs" in rule:
        count = min(count, rule["runs"] - runs_completed)
    result = _unbounded_occurrences(rule, after=after, count=count, watcher=watcher)
    return tuple(value for value in result
                 if "end_utc" not in rule or value <= _utc(rule["end_utc"]))


def schedule_preview(rule: Mapping[str, Any], *, after: datetime,
                     watcher: bool = False) -> tuple[datetime, ...]:
    first = _unbounded_occurrences(rule, after=after, count=1, watcher=watcher)
    if not first:
        return ()
    validate_end(rule, first[0])
    if "runs" not in rule and "end_utc" not in rule:
        return next_occurrences(rule, after=after, watcher=watcher)
    times = next_occurrences(rule, after=after, count=10, watcher=watcher)
    if rule.get("runs", 0) > 10:
        return times[:3]
    if "end_utc" in rule and len(times) == 10 and next_occurrences(
        rule, after=times[-1], count=1, watcher=watcher,
    ):
        return times[:3]
    return times


def remaining_runs(rule: Mapping[str, Any], *, next_at: float | None,
                   runs_completed: int = 0, now: datetime) -> int | None:
    if schedule_expired(rule, now, runs_completed) or next_at is None:
        return 0
    if "runs" in rule:
        return max(0, rule["runs"] - runs_completed)
    if "end_utc" not in rule:
        return None
    due = datetime.fromtimestamp(next_at, timezone.utc)
    first = due if due > now else _unbounded_occurrences(rule, after=now, count=1)[0]
    end = _utc(rule["end_utc"])
    pending = int(due <= now)
    if first > end:
        return pending
    if rule["kind"] == "interval":
        return pending + int((end - first).total_seconds() // rule["seconds"]) + 1
    zone = ZoneInfo(rule["timezone"])
    start_day, end_day = first.astimezone(zone), end.astimezone(zone)
    if rule["kind"] == "weekly":
        span = (end_day.date() - start_day.date()).days
        total = sum(max(0, (span - (day - start_day.weekday()) % 7) // 7 + 1)
                    for day in set(rule["days"]))
        last_selected = end_day.weekday() in rule["days"]
    else:
        total = 0
        month = start_day.year * 12 + start_day.month - 1
        final = end_day.year * 12 + end_day.month - 1
        while month <= final:
            year, index = divmod(month, 12)
            days = {min(day, calendar.monthrange(year, index + 1)[1]) for day in rule["days"]}
            lower = start_day.day if month == start_day.year * 12 + start_day.month - 1 else 1
            upper = end_day.day if month == final else 31
            total += sum(lower <= day <= upper for day in days)
            month += 1
        last_selected = end_day.day in {
            min(day, calendar.monthrange(end_day.year, end_day.month)[1]) for day in rule["days"]
        }
    hour, minute = _clock(rule["time"])
    if last_selected and _local_candidate(end_day, hour=hour, minute=minute, zone=zone) > end:
        total -= 1
    return total + pending
