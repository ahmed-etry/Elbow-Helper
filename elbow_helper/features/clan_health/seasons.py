"""Season-window helpers for clan health."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Tuple

from .config import SEASON_KEY_RE, UTC


def _season_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"

def _parse_season_key(value: str) -> Optional[Tuple[int, int]]:
    match = SEASON_KEY_RE.fullmatch((value or "").strip())
    if not match:
        return None
    year = int(match.group("year"))
    month = int(match.group("month"))
    if month < 1 or month > 12:
        return None
    return year, month

def _prev_month(year: int, month: int) -> Tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1

def _next_month(year: int, month: int) -> Tuple[int, int]:
    if month == 12:
        return year + 1, 1
    return year, month + 1

def _season_end_dt(year: int, month: int) -> datetime:
    # CWL cycle cutoff for reporting windows.
    return datetime(year, month, 11, 8, 0, 0, tzinfo=UTC)

def _latest_completed_season_key(now: datetime) -> str:
    current_end = _season_end_dt(now.year, now.month)
    if now >= current_end:
        return _season_key(now.year, now.month)
    py, pm = _prev_month(now.year, now.month)
    return _season_key(py, pm)

def _season_key_for_datetime(value: datetime) -> str:
    """Map an arbitrary timestamp into the clan-health season key."""
    current_end = _season_end_dt(value.year, value.month)
    if value < current_end:
        return _season_key(value.year, value.month)
    ny, nm = _next_month(value.year, value.month)
    return _season_key(ny, nm)

def _clan_games_signal_open(*, cycle_start: datetime, cycle_end: datetime) -> bool:
    # CG runs day 22 00:00 UTC to day 28 00:00 UTC each month.
    # Return True only if the cycle window overlaps with at least one such band.
    year, month = cycle_start.year, cycle_start.month
    while True:
        cg_band_start = datetime(year, month, 22, 0, 0, 0, tzinfo=UTC)
        cg_band_end = datetime(year, month, 28, 0, 0, 0, tzinfo=UTC)
        if cg_band_start < cycle_end and cg_band_end > cycle_start:
            return True
        # Advance to next month
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
        if datetime(year, month, 1, tzinfo=UTC) > cycle_end:
            break
    return False

def _clan_games_window_state(*, cycle_start: datetime, cycle_end: datetime) -> str:
    # Used by player health to avoid treating a window with no CG event
    # like a weak CG result.
    year, month = cycle_start.year, cycle_start.month
    saw_overlap = False
    saw_full_band = False
    while True:
        cg_band_start = datetime(year, month, 22, 0, 0, 0, tzinfo=UTC)
        cg_band_end = datetime(year, month, 28, 0, 0, 0, tzinfo=UTC)
        if cg_band_start < cycle_end and cg_band_end > cycle_start:
            saw_overlap = True
            if cg_band_start >= cycle_start and cg_band_end <= cycle_end:
                saw_full_band = True
                break
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
        if datetime(year, month, 1, tzinfo=UTC) > cycle_end:
            break
    if saw_full_band:
        return "Active"
    if saw_overlap:
        return "Partial"
    return "N/A"


def _raid_weekend_window_state(*, cycle_start: datetime, cycle_end: datetime) -> str:
    # Raid Weekend runs Friday 07:00 UTC to Monday 07:00 UTC.
    reference = cycle_start.astimezone(UTC)
    days_back_to_friday = (reference.weekday() - 4) % 7
    weekend_start = (reference - timedelta(days=days_back_to_friday)).replace(
        hour=7,
        minute=0,
        second=0,
        microsecond=0,
        tzinfo=UTC,
    )
    saw_overlap = False
    saw_full_band = False
    while weekend_start < cycle_end:
        weekend_end = weekend_start + timedelta(days=3)
        if weekend_start < cycle_end and weekend_end > cycle_start:
            saw_overlap = True
            if weekend_start >= cycle_start and weekend_end <= cycle_end:
                saw_full_band = True
                break
        weekend_start += timedelta(days=7)
    if saw_full_band:
        return "Active"
    if saw_overlap:
        return "Partial"
    return "N/A"
