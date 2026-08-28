"""Availability overlap math and concrete timestamp rendering."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from .logic import DAY_ORDER
from .logic import _has_explicit_date
from .logic import _hours_to_time
from .logic import _normalize_availability_text
from .logic import _normalize_structured_windows
from .logic import _parse_date_from_text
from .logic import _parse_time_token
from .logic import _resolve_timezone_offset
from .logic import _strip_markdown
from .logic import parse_availability_windows


def _normalize_ranges(
    days: Set[str],
    start: float,
    end: float,
    offset_hours: float,
) -> List[Tuple[int, float, float]]:
    ranges: List[Tuple[int, float, float]] = []
    if end == start:
        return ranges
    for day in days:
        if day not in DAY_ORDER:
            continue
        day_idx = DAY_ORDER.index(day)
        if end <= start:
            ranges.extend(_normalize_ranges({day}, start, 24.0, offset_hours))
            next_day = DAY_ORDER[(day_idx + 1) % 7]
            ranges.extend(_normalize_ranges({next_day}, 0.0, end, offset_hours))
            continue

        start_utc = start - offset_hours
        end_utc = end - offset_hours
        while start_utc < 0:
            start_utc += 24
            end_utc += 24
            day_idx = (day_idx - 1) % 7
        while start_utc >= 24:
            start_utc -= 24
            end_utc -= 24
            day_idx = (day_idx + 1) % 7
        if end_utc <= 24:
            ranges.append((day_idx, start_utc, end_utc))
        else:
            ranges.append((day_idx, start_utc, 24.0))
            ranges.append(((day_idx + 1) % 7, 0.0, end_utc - 24.0))
    return ranges


def _ranges_from_structured_windows(
    windows: List[Dict[str, Any]],
) -> List[Tuple[int, float, float]]:
    ranges: List[Tuple[int, float, float]] = []
    for window in _normalize_structured_windows(windows):
        days = set(window.get("days") or [])
        start = window.get("start")
        end = window.get("end")
        timezone_text = window.get("timezone") or "UTC"
        if not days or start is None or end is None:
            continue
        offset = _resolve_timezone_offset(timezone_text, "UTC")
        ranges.extend(_normalize_ranges(days, float(start), float(end), offset))
    return ranges


def availability_matches(applicant_text: str, examiner_text: str, examiner_timezone: str) -> bool:
    app_windows = parse_availability_windows(applicant_text)
    ex_windows = parse_availability_windows(examiner_text)
    if not app_windows or not ex_windows:
        return False
    app_offset = _resolve_timezone_offset(applicant_text, "UTC")
    examiner_offset = _resolve_timezone_offset(examiner_text, examiner_timezone)
    app_ranges: List[Tuple[int, float, float]] = []
    for app_days, (app_start, app_end) in app_windows:
        if not app_days:
            continue
        app_ranges.extend(_normalize_ranges(app_days, app_start, app_end, app_offset))
    ex_ranges: List[Tuple[int, float, float]] = []
    for ex_days, (ex_start, ex_end) in ex_windows:
        if not ex_days:
            continue
        ex_ranges.extend(_normalize_ranges(ex_days, ex_start, ex_end, examiner_offset))
    for app_day, app_start, app_end in app_ranges:
        for ex_day, ex_start, ex_end in ex_ranges:
            if app_day != ex_day:
                continue
            if app_end <= ex_start or ex_end <= app_start:
                continue
            return True
    return False


def availability_matches_structured(
    applicant_windows: List[Dict[str, Any]],
    examiner_text: str,
    examiner_timezone: str,
) -> bool:
    if not applicant_windows:
        return False
    ex_windows = parse_availability_windows(examiner_text)
    if not ex_windows:
        return False
    examiner_offset = _resolve_timezone_offset(examiner_text, examiner_timezone)
    app_ranges = _ranges_from_structured_windows(applicant_windows)
    if not app_ranges:
        return False
    ex_ranges: List[Tuple[int, float, float]] = []
    for ex_days, (ex_start, ex_end) in ex_windows:
        if not ex_days:
            continue
        ex_ranges.extend(_normalize_ranges(ex_days, ex_start, ex_end, examiner_offset))
    for app_day, app_start, app_end in app_ranges:
        for ex_day, ex_start, ex_end in ex_ranges:
            if app_day != ex_day:
                continue
            if app_end <= ex_start or ex_end <= app_start:
                continue
            return True
    return False


def _parse_explicit_date_window(text: str) -> Optional[Tuple[datetime, datetime]]:
    normalized = _normalize_availability_text(text)
    date_dt = _parse_date_from_text(normalized)
    if not date_dt:
        return None
    lowered = normalized.lower()
    import re

    time_match = re.search(
        r"(\d{1,2}(?::\d{2})?|\d{3,4}|noon|midnight)\s*(a\.?\s*m\.?|p\.?\s*m\.?|am|pm)?\s*"
        r"(?:-|to)\s*"
        r"(\d{1,2}(?::\d{2})?|\d{3,4}|noon|midnight)\s*(a\.?\s*m\.?|p\.?\s*m\.?|am|pm)?",
        lowered,
        flags=re.IGNORECASE,
    )
    if not time_match:
        return None
    start_token = time_match.group(1)
    end_token = time_match.group(3)
    start_ampm = time_match.group(2)
    end_ampm = time_match.group(4)
    if not start_ampm and end_ampm:
        start_ampm = end_ampm
    start_hours = _parse_time_token(start_token, start_ampm)
    end_hours = _parse_time_token(end_token, end_ampm)
    if start_hours is None or end_hours is None:
        return None
    start_hour = int(start_hours)
    start_minute = int(round((start_hours - start_hour) * 60))
    end_hour = int(end_hours)
    end_minute = int(round((end_hours - end_hour) * 60))
    offset = _resolve_timezone_offset(normalized, "UTC")
    start_local = datetime(date_dt.year, date_dt.month, date_dt.day, start_hour, start_minute)
    end_local = datetime(date_dt.year, date_dt.month, date_dt.day, end_hour, end_minute)
    if end_local <= start_local:
        end_local += timedelta(days=1)
    start_utc = (start_local - timedelta(hours=offset)).replace(tzinfo=timezone.utc)
    end_utc = (end_local - timedelta(hours=offset)).replace(tzinfo=timezone.utc)
    return start_utc, end_utc


def _next_window_for_range(
    days: Set[str],
    start: float,
    end: float,
    offset: float,
    *,
    now: Optional[datetime] = None,
) -> Optional[Tuple[datetime, datetime]]:
    if not days:
        return None
    ordered = [day for day in DAY_ORDER if day in days]
    idxs = [DAY_ORDER.index(day) for day in ordered]
    is_contiguous = bool(idxs) and idxs == list(range(min(idxs), max(idxs) + 1))
    current = now or datetime.now(timezone.utc)
    if is_contiguous:
        local_now = current + timedelta(hours=offset)
        start_idx = min(idxs)
        end_idx = max(idxs)
        days_ahead = (start_idx - local_now.weekday()) % 7
        start_date = (local_now + timedelta(days=days_ahead)).date()
        start_local = datetime.combine(start_date, _hours_to_time(start))
        if start_local <= local_now:
            start_date = start_date + timedelta(days=7)
            start_local = datetime.combine(start_date, _hours_to_time(start))
        end_date = start_date + timedelta(days=end_idx - start_idx)
        end_local = datetime.combine(end_date, _hours_to_time(end))
        if end_local <= start_local:
            end_local += timedelta(days=1)
        start_dt = (start_local - timedelta(hours=offset)).replace(tzinfo=timezone.utc)
        end_dt = (end_local - timedelta(hours=offset)).replace(tzinfo=timezone.utc)
        return start_dt, end_dt
    ranges = _normalize_ranges(days, start, end, offset)
    if not ranges:
        return None
    today_idx = current.weekday()
    best_start: Optional[datetime] = None
    best_end: Optional[datetime] = None
    for day_idx, start_h, end_h in ranges:
        days_ahead = (day_idx - today_idx) % 7
        start_date = (current + timedelta(days=days_ahead)).date()
        start_dt = datetime.combine(start_date, _hours_to_time(start_h))
        if start_dt < current:
            start_dt += timedelta(days=7)
        end_dt = datetime.combine(start_dt.date(), _hours_to_time(end_h))
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        if not best_start or start_dt < best_start:
            best_start = start_dt
            best_end = end_dt
    if not best_start or not best_end:
        return None
    return best_start, best_end


def _next_day_window(
    day: str,
    start: float,
    end: float,
    offset: float,
    *,
    now: Optional[datetime] = None,
) -> Optional[Tuple[datetime, datetime]]:
    if day not in DAY_ORDER:
        return None
    current = now or datetime.now(timezone.utc)
    local_now = current + timedelta(hours=offset)
    day_idx = DAY_ORDER.index(day)
    days_ahead = (day_idx - local_now.weekday()) % 7
    start_date = (local_now + timedelta(days=days_ahead)).date()
    start_local = datetime.combine(start_date, _hours_to_time(start))
    if start_local <= local_now:
        start_local += timedelta(days=7)
    end_local = datetime.combine(start_local.date(), _hours_to_time(end))
    if end_local <= start_local:
        end_local += timedelta(days=1)
    start_dt = (start_local - timedelta(hours=offset)).replace(tzinfo=timezone.utc)
    end_dt = (end_local - timedelta(hours=offset)).replace(tzinfo=timezone.utc)
    return start_dt, end_dt


def _next_overlap_window(
    applicant_text: str,
    examiner_text: str,
    examiner_timezone: str,
) -> Optional[Tuple[datetime, datetime]]:
    app_windows = parse_availability_windows(applicant_text)
    ex_windows = parse_availability_windows(examiner_text)
    if not app_windows or not ex_windows:
        return None
    app_offset = _resolve_timezone_offset(applicant_text, "UTC")
    ex_offset = _resolve_timezone_offset(examiner_text, examiner_timezone)
    app_ranges: List[Tuple[int, float, float]] = []
    for app_days, (app_start, app_end) in app_windows:
        if not app_days:
            continue
        app_ranges.extend(_normalize_ranges(app_days, app_start, app_end, app_offset))
    ex_ranges: List[Tuple[int, float, float]] = []
    for ex_days, (ex_start, ex_end) in ex_windows:
        if not ex_days:
            continue
        ex_ranges.extend(_normalize_ranges(ex_days, ex_start, ex_end, ex_offset))
    overlaps: List[Tuple[int, float, float]] = []
    for app_day, app_start, app_end in app_ranges:
        for ex_day, ex_start, ex_end in ex_ranges:
            if app_day != ex_day:
                continue
            start_value = max(app_start, ex_start)
            end_value = min(app_end, ex_end)
            if start_value < end_value:
                overlaps.append((app_day, start_value, end_value))
    if not overlaps:
        return None
    now = datetime.now(timezone.utc)
    candidates: List[Tuple[datetime, datetime]] = []
    for day_idx, start_h, end_h in overlaps:
        days_ahead = (day_idx - now.weekday()) % 7
        start_date = (now + timedelta(days=days_ahead)).date()
        start_dt = datetime.combine(start_date, _hours_to_time(start_h))
        end_dt = datetime.combine(start_date, _hours_to_time(end_h))
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        if end_dt <= now:
            start_dt += timedelta(days=7)
            end_dt += timedelta(days=7)
        candidates.append((start_dt, end_dt))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0]


def _all_overlap_windows_structured(
    applicant_windows: List[Dict[str, Any]],
    examiner_text: str,
    examiner_timezone: str,
) -> List[Tuple[datetime, datetime]]:
    if not applicant_windows:
        return []
    ex_windows = parse_availability_windows(examiner_text)
    if not ex_windows:
        return []
    ex_offset = _resolve_timezone_offset(examiner_text, examiner_timezone)
    app_ranges = _ranges_from_structured_windows(applicant_windows)
    if not app_ranges:
        return []
    ex_ranges: List[Tuple[int, float, float]] = []
    for ex_days, (ex_start, ex_end) in ex_windows:
        if not ex_days:
            continue
        ex_ranges.extend(_normalize_ranges(ex_days, ex_start, ex_end, ex_offset))
    overlaps: List[Tuple[int, float, float]] = []
    for app_day, app_start, app_end in app_ranges:
        for ex_day, ex_start, ex_end in ex_ranges:
            if app_day != ex_day:
                continue
            start_value = max(app_start, ex_start)
            end_value = min(app_end, ex_end)
            if start_value < end_value:
                overlaps.append((app_day, start_value, end_value))
    if not overlaps:
        return []
    now = datetime.now(timezone.utc)
    candidates: List[Tuple[datetime, datetime]] = []
    for day_idx, start_h, end_h in overlaps:
        days_ahead = (day_idx - now.weekday()) % 7
        start_date = (now + timedelta(days=days_ahead)).date()
        start_dt = datetime.combine(start_date, _hours_to_time(start_h))
        if start_dt < now:
            start_dt += timedelta(days=7)
        end_dt = datetime.combine(start_dt.date(), _hours_to_time(end_h))
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        candidates.append((start_dt, end_dt))
    candidates.sort(key=lambda item: item[0])
    unique: List[Tuple[datetime, datetime]] = []
    seen: Set[Tuple[int, int]] = set()
    for start_dt, end_dt in candidates:
        key = (int(start_dt.timestamp()), int(end_dt.timestamp()))
        if key in seen:
            continue
        seen.add(key)
        unique.append((start_dt, end_dt))
    return unique


def _format_structured_availability_examples(
    windows: List[Dict[str, Any]],
    *,
    limit: int = 3,
) -> str:
    normalized_windows = _normalize_structured_windows(windows)
    if not normalized_windows or limit <= 0:
        return ""
    now = datetime.now(timezone.utc)
    display: List[Tuple[datetime, datetime]] = []
    for window in normalized_windows:
        days = set(window.get("days") or [])
        start = window.get("start")
        end = window.get("end")
        timezone_text = window.get("timezone") or "UTC"
        if not days or start is None or end is None:
            continue
        offset = _resolve_timezone_offset(timezone_text, "UTC")
        for day in DAY_ORDER:
            if day not in days:
                continue
            next_window = _next_day_window(day, float(start), float(end), offset, now=now)
            if next_window:
                display.append(next_window)
    if not display:
        return ""
    display.sort(key=lambda item: item[0])
    limited = display[:limit]
    lines = [
        f"<t:{int(start_dt.timestamp())}:F> - <t:{int(end_dt.timestamp())}:F>"
        for start_dt, end_dt in limited
    ]
    if len(display) > limit:
        remaining = len(display) - limit
        if remaining == 1:
            lines.append("...plus 1 more available time")
        else:
            lines.append(f"...plus {remaining} more available times")
    return "\n".join(lines)


def _format_ticket_availability_display(text: str) -> str:
    cleaned = _strip_markdown(_normalize_availability_text(text))
    if not cleaned:
        return ""
    if _has_explicit_date(cleaned):
        window = _parse_explicit_date_window(cleaned)
        if window:
            start_dt, end_dt = window
            return f"<t:{int(start_dt.timestamp())}:F> - <t:{int(end_dt.timestamp())}:F>"
        return ""
    windows = parse_availability_windows(cleaned)
    if not windows:
        return ""
    offset = _resolve_timezone_offset(cleaned, "UTC")
    now = datetime.now(timezone.utc)
    display: List[Tuple[datetime, datetime]] = []
    for days, (start, end) in windows:
        window = _next_window_for_range(days, start, end, offset, now=now)
        if window:
            display.append(window)
    if not display:
        return ""
    display.sort(key=lambda item: item[0])
    return "\n".join(
        f"<t:{int(start_dt.timestamp())}:F> - <t:{int(end_dt.timestamp())}:F>"
        for start_dt, end_dt in display
    )


__all__ = [
    "_all_overlap_windows_structured",
    "_format_structured_availability_examples",
    "_format_ticket_availability_display",
    "_next_overlap_window",
    "availability_matches",
    "availability_matches_structured",
]
