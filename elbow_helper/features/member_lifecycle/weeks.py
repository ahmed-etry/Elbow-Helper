"""Recruitment weeks: Monday 00:00 UTC to Sunday 23:59:59 UTC, named by the month holding most of their days."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

WEEK = timedelta(days=7)


def week_start(moment: datetime) -> datetime:
    """Monday 00:00 UTC of the week containing the moment."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    return (moment - timedelta(days=moment.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def week_label(start: datetime) -> str:
    """`Week N · Month Year`; a week belongs to the month of its Thursday, which holds more than half of its days."""
    thursday = start + timedelta(days=3)
    return f"Week {(thursday.day - 1) // 7 + 1} · {thursday:%B %Y}"


def month_bounds(moment: datetime) -> tuple[datetime, datetime]:
    """1st 00:00 UTC of the calendar month containing the moment, and of the month after it."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    following = (start + timedelta(days=32)).replace(day=1)
    return start, following


def ordinal(number: int) -> str:
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"
