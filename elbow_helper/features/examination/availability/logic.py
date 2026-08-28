"""Availability parsing, formatting, and structured state helpers."""

from __future__ import annotations

import re
from datetime import datetime, time, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import discord

from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX, DEFAULT_THUMBNAIL_URL
from elbow_helper.domain.timezones import TIMEZONE_ENTRIES
from elbow_helper.domain.timezones import format_timezone_display
from elbow_helper.infrastructure.time import resolve_timezone

DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_ALIASES = {
    "monday": "mon",
    "mon": "mon",
    "tuesday": "tue",
    "tue": "tue",
    "tues": "tue",
    "wednesday": "wed",
    "wed": "wed",
    "thursday": "thu",
    "thu": "thu",
    "thur": "thu",
    "thurs": "thu",
    "friday": "fri",
    "fri": "fri",
    "saturday": "sat",
    "sat": "sat",
    "sunday": "sun",
    "sun": "sun",
}

TZ_ABBREVIATIONS = {
    "est": -5,
    "edt": -4,
    "cst": -6,
    "cdt": -5,
    "mst": -7,
    "mdt": -6,
    "pst": -8,
    "pdt": -7,
    "cet": 1,
    "cest": 2,
    "bst": 1,
    "ist": 5.5,
    "jst": 9,
    "aest": 10,
    "aedt": 11,
    "acst": 9.5,
}

MONTH_ALIASES = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def _match_curated_timezone(text: str) -> Optional[str]:
    lowered = _normalize_availability_text(text).lower()
    for label, zone_name in TIMEZONE_ENTRIES:
        if re.search(rf"(?<!\w){re.escape(zone_name.lower())}(?!\w)", lowered):
            return zone_name
        if label != "UTC" and re.search(rf"(?<!\w){re.escape(label.lower())}(?!\w)", lowered):
            return zone_name
    if re.search(r"(?<!\w)utc(?![+\-\d:])", lowered):
        return "UTC"
    return None


def _has_explicit_date(text: str) -> bool:
    normalized = _normalize_availability_text(text).lower()
    if re.search(r"\b\d{4}-\d{1,2}-\d{1,2}\b", normalized):
        return True
    if re.search(
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s*\d{4})?\b",
        normalized,
    ):
        return True
    return False


def _parse_date_from_text(text: str) -> Optional[datetime]:
    normalized = _normalize_availability_text(text).lower()
    iso_match = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", normalized)
    if iso_match:
        year, month, day = (int(part) for part in iso_match.groups())
        try:
            return datetime(year, month, day)
        except ValueError:
            return None

    named_match = re.search(
        r"\b(?P<month>jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+"
        r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,\s*(?P<year>\d{4}))?\b",
        normalized,
    )
    if not named_match:
        return None

    month = MONTH_ALIASES.get(named_match.group("month"))
    day = int(named_match.group("day"))
    year_text = named_match.group("year")
    year = int(year_text) if year_text else datetime.now().year
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def _normalize_availability_text(text: str) -> str:
    # Normalize free-text availability for tolerant parsing.
    normalized = _normalize_text(text)
    normalized = re.sub(r"(\d{1,2})h(\d{2})", r"\1:\2", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"(\d{1,2})h\b", r"\1:00", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"(\d{1,2})\.(\d{2})", r"\1:\2", normalized)
    normalized = re.sub(r"\b(a|p)\s*\.?\s*m\.?\b", r"\1m", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bmost\s+days\b", "daily", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bweek\s*days\b", "weekdays", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bweek\s*ends\b", "weekends", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\buntil\b", "to", normalized, flags=re.IGNORECASE)
    return normalized


def _parse_time(token: str, ampm: Optional[str]) -> Optional[float]:
    try:
        parts = token.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return None
    if ampm:
        ampm = ampm.lower().replace(".", "").replace(" ", "")
        if hour == 12:
            hour = 0
        if ampm == "pm":
            hour += 12
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return hour + minute / 60


def _parse_time_token(token: str, ampm: Optional[str] = None) -> Optional[float]:
    # Parse flexible time tokens: 16:30, 1630, 4pm, noon, midnight.
    cleaned = token.strip().lower()
    if not cleaned:
        return None
    cleaned = cleaned.replace(".", "")
    cleaned = re.sub(r"\s+", "", cleaned)
    if cleaned in {"noon"}:
        return 12.0
    if cleaned in {"midnight"}:
        return 0.0
    local_ampm = ampm
    token_ampm_match = re.match(r"^(.*?)(am|pm)$", cleaned)
    if token_ampm_match:
        cleaned = token_ampm_match.group(1)
        local_ampm = token_ampm_match.group(2)
    if cleaned.isdigit() and len(cleaned) in {3, 4}:
        hour = int(cleaned[:-2])
        minute = int(cleaned[-2:])
        return _parse_time(f"{hour}:{minute:02d}", local_ampm)
    if cleaned.isdigit():
        return _parse_time(f"{int(cleaned)}:00", local_ampm)
    if re.fullmatch(r"\d{1,2}:\d{2}", cleaned):
        return _parse_time(cleaned, local_ampm)
    return None


def _parse_single_time_input(value: str) -> Optional[float]:
    # Parse a single time value like 16:00, 4pm, 1630, or noon.
    if not value:
        return None
    normalized = _normalize_availability_text(value).lower().replace(".", "").strip()
    match = re.fullmatch(
        r"\s*(\d{1,2}(?::\d{2})?|\d{3,4}|noon|midnight)\s*(am|pm)?\s*",
        normalized,
    )
    if not match:
        return None
    return _parse_time_token(match.group(1), match.group(2))


def _extract_days(text: str) -> Set[str]:
    # Pull day tokens and ranges from a text slice.
    days: Set[str] = set()
    if re.search(r"\bweekend(s)?\b", text):
        days.update({"sat", "sun"})
    if re.search(r"\bweekday(s)?\b", text):
        days.update({"mon", "tue", "wed", "thu", "fri"})
    if re.search(r"\b(daily|every\s+day|everyday|any\s+day|all\s+week|most\s+days)\b", text):
        days.update(DAY_ORDER)
    for raw, canon in DAY_ALIASES.items():
        if re.search(rf"\b{re.escape(raw)}\b", text):
            days.add(canon)
    range_match = re.search(
        r"(mon|monday|tue|tuesday|wed|wednesday|thu|thursday|fri|friday|sat|saturday|sun|sunday)"
        r"\s*(?:-|to)\s*"
        r"(mon|monday|tue|tuesday|wed|wednesday|thu|thursday|fri|friday|sat|saturday|sun|sunday)",
        text,
    )
    if range_match:
        start = DAY_ALIASES.get(range_match.group(1), range_match.group(1)[:3])
        end = DAY_ALIASES.get(range_match.group(2), range_match.group(2)[:3])
        if start in DAY_ORDER and end in DAY_ORDER:
            start_idx = DAY_ORDER.index(start)
            end_idx = DAY_ORDER.index(end)
            if start_idx <= end_idx:
                days.update(DAY_ORDER[start_idx : end_idx + 1])
            else:
                days.update(DAY_ORDER[start_idx:])
                days.update(DAY_ORDER[: end_idx + 1])
    return days


def parse_availability_windows(text: str) -> List[Tuple[Set[str], Tuple[float, float]]]:
    # Parse one or more day + time windows from a freeform availability string.
    if not text:
        return []
    normalized = _normalize_availability_text(text)
    lowered = normalized.lower()
    time_re = re.compile(
        r"(\d{1,2}(?::\d{2})?|\d{3,4}|noon|midnight)\s*(a\.?\s*m\.?|p\.?\s*m\.?|am|pm)?\s*"
        r"(?:-|to)\s*"
        r"(\d{1,2}(?::\d{2})?|\d{3,4}|noon|midnight)\s*(a\.?\s*m\.?|p\.?\s*m\.?|am|pm)?",
        flags=re.IGNORECASE,
    )
    matches = list(time_re.finditer(lowered))
    if not matches:
        return []
    windows: List[Tuple[Set[str], Tuple[float, float]]] = []
    global_days = _extract_days(lowered)
    last_days: Optional[Set[str]] = None
    last_idx = 0
    for match in matches:
        segment = lowered[last_idx:match.start()]
        days = _extract_days(segment)
        if not days and last_days:
            days = set(last_days)
        if not days:
            days = global_days or set(DAY_ORDER)
        start_token = match.group(1)
        end_token = match.group(3)
        start_ampm = match.group(2)
        end_ampm = match.group(4)
        if not start_ampm and end_ampm:
            start_ampm = end_ampm
        start = _parse_time_token(start_token, start_ampm)
        end = _parse_time_token(end_token, end_ampm)
        if start is None or end is None:
            last_idx = match.end()
            continue
        windows.append((days or set(DAY_ORDER), (start, end)))
        last_days = set(days)
        last_idx = match.end()
    return windows


def parse_timezone_offset(value: str) -> Optional[float]:
    # Parse timezone offsets like UTC-5, GMT+2, or common abbreviations.
    if not value:
        return None
    curated_zone = _match_curated_timezone(value)
    if curated_zone:
        tzinfo = resolve_timezone(curated_zone)
        if tzinfo is not None:
            offset = datetime.now(timezone.utc).astimezone(tzinfo).utcoffset()
            if offset is not None:
                return offset.total_seconds() / 3600
    lowered = _normalize_availability_text(value).lower()
    match = re.search(r"\b(utc|gmt)(?:\s*([+-])?\s*(\d{1,2})(?::(\d{2}))?)?\b", lowered)
    if match:
        if not match.group(3):
            return 0.0
        sign = -1 if match.group(2) == "-" else 1
        try:
            hours = int(match.group(3))
            minutes = int(match.group(4) or 0)
        except ValueError:
            return None
        return sign * (hours + minutes / 60)
    for abbr, offset in TZ_ABBREVIATIONS.items():
        if re.search(rf"\b{re.escape(abbr)}\b", lowered):
            return float(offset)
    return None


def _resolve_timezone_offset(value: str, fallback: Optional[str] = None) -> float:
    # Resolve timezone offset with optional fallback source.
    offset = parse_timezone_offset(value)
    if offset is not None:
        return offset
    if fallback:
        fallback_offset = parse_timezone_offset(fallback)
        if fallback_offset is not None:
            return fallback_offset
    return 0.0


_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_PUNCT_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": "\"",
        "\u201d": "\"",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u00a0": " ",
    }
)


def _strip_invisible(text: str) -> str:
    # Remove zero-width characters and normalize whitespace.
    text = _ZERO_WIDTH_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_text(text: str) -> str:
    # Normalize punctuation and spaces for reliable comparisons.
    return _strip_invisible(text).translate(_PUNCT_TRANSLATION)


def _strip_markdown(text: str) -> str:
    # Remove common markdown markers for matching.
    cleaned = re.sub(r"[`*_]", "", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def _clean_answer(text: str) -> str:
    # Strip code fences and extra whitespace from answers.
    cleaned = _normalize_text(text)
    cleaned = cleaned.replace("```", "")
    cleaned = cleaned.replace("`", "")
    return _strip_markdown(cleaned)


def _normalize_question(text: str) -> str:
    # Normalize question text for matching.
    cleaned = _normalize_text(text)
    cleaned = _strip_markdown(cleaned).lower()
    return cleaned.strip().rstrip(":").rstrip("?")


def _hours_to_time(hours: float) -> time:
    # Convert fractional hours to a UTC time object.
    hour = int(hours)
    minutes = int(round((hours - hour) * 60))
    if minutes == 60:
        hour += 1
        minutes = 0
    hour %= 24
    return time(hour=hour, minute=minutes, tzinfo=timezone.utc)


def _extract_timezone_label(text: str) -> str:
    # Preserve explicit timezone labels when present.
    normalized = _normalize_text(text)
    curated_zone = _match_curated_timezone(normalized)
    if curated_zone:
        return curated_zone
    match = re.search(r"\b(UTC|GMT)\s*([+-])?\s*(\d{1,2})(?::(\d{2}))?\b", normalized, flags=re.IGNORECASE)
    if match:
        label = match.group(1).upper()
        sign = match.group(2) or "+"
        hours = match.group(3)
        minutes = match.group(4)
        offset = f"{sign}{hours}"
        if minutes:
            offset = f"{offset}:{minutes}"
        return f"{label}{offset}"
    lowered = normalized.lower()
    for abbr in TZ_ABBREVIATIONS.keys():
        if re.search(rf"\b{re.escape(abbr)}\b", lowered):
            return abbr.upper()
    return "UTC"


def _format_time_range(start: float, end: float) -> str:
    # Format a time range as HH:MM-HH:MM.
    start_time = _hours_to_time(start)
    end_time = _hours_to_time(end)
    return f"{start_time:%H:%M}-{end_time:%H:%M}"


def _build_availability_string(days: Set[str], start: float, end: float, timezone_text: str) -> str:
    # Build a consistent availability string for matching/parsing.
    days_text = _format_days(days) or "Daily"
    return f"{days_text} {_format_time_range(start, end)} {timezone_text}"


def _ordered_days(days: List[str] | Set[str] | Tuple[str, ...]) -> List[str]:
    day_set = set(days or [])
    return [day for day in DAY_ORDER if day in day_set]


def _normalize_structured_windows(windows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    canonical: List[Dict[str, Any]] = []
    for window in windows:
        days = _ordered_days(window.get("days") or [])
        start = window.get("start")
        end = window.get("end")
        timezone_text = str(window.get("timezone") or "UTC").strip() or "UTC"
        if not days or start is None or end is None:
            continue
        try:
            start_value = float(start)
            end_value = float(end)
        except (TypeError, ValueError):
            continue
        canonical.append(
            {
                "days": days,
                "start": start_value,
                "end": end_value,
                "timezone": timezone_text,
            }
        )

    canonical.sort(
        key=lambda window: (
            tuple(DAY_ORDER.index(day) for day in window["days"]),
            window["timezone"],
            window["start"],
            window["end"] if window["end"] > window["start"] else window["end"] + 24.0,
        )
    )

    merged: List[Dict[str, Any]] = []
    for window in canonical:
        start_value = float(window["start"])
        end_value = float(window["end"])
        normalized_end = end_value if end_value > start_value else end_value + 24.0
        if not merged:
            merged.append(dict(window))
            continue
        previous = merged[-1]
        if previous["days"] != window["days"] or previous["timezone"] != window["timezone"]:
            merged.append(dict(window))
            continue
        prev_start = float(previous["start"])
        prev_end = float(previous["end"])
        prev_normalized_end = prev_end if prev_end > prev_start else prev_end + 24.0
        if start_value <= prev_normalized_end:
            new_end = max(prev_normalized_end, normalized_end)
            previous["end"] = new_end if new_end <= 24.0 else new_end - 24.0
            continue
        merged.append(dict(window))
    return merged


def _format_structured_availability_display(
    windows: List[Dict[str, Any]],
    *,
    indexed: bool = False,
) -> str:
    # Render structured windows as normalized recurring availability lines.
    normalized_windows = _normalize_structured_windows(windows)
    if not normalized_windows:
        return ""
    lines: List[str] = []
    for index, window in enumerate(normalized_windows, start=1):
        days = set(window.get("days") or [])
        start = window.get("start")
        end = window.get("end")
        timezone_text = window.get("timezone") or "UTC"
        if not days or start is None or end is None:
            continue
        line = _build_availability_string(
            days,
            float(start),
            float(end),
            format_timezone_display(str(timezone_text)),
        )
        if indexed:
            line = f"{index}. {line}"
        lines.append(line)
    return "\n".join(lines)


def _canonicalize_availability_text(
    raw_text: str,
    timezone_fallback: str = "UTC",
    *,
    allow_input_timezone: bool = True,
) -> Optional[str]:
    # Normalize free-text availability into canonical windows.
    windows = parse_availability_windows(raw_text)
    if not windows:
        return None
    explicit_tz = parse_timezone_offset(raw_text)
    timezone_text = (
        _extract_timezone_label(raw_text)
        if allow_input_timezone and explicit_tz is not None
        else (timezone_fallback or "UTC")
    )
    parts = [
        _build_availability_string(days, start, end, timezone_text)
        for days, (start, end) in windows
    ]
    return " | ".join(parts) if parts else None


def _format_days(days: Set[str]) -> str:
    # Format days into a readable range or list.
    if not days:
        return ""
    if len(days) == 7:
        return "Daily"
    ordered = [d for d in DAY_ORDER if d in days]
    if not ordered:
        return ""
    idxs = [DAY_ORDER.index(d) for d in ordered]
    is_contiguous = idxs == list(range(min(idxs), max(idxs) + 1))
    label_map = {
        "mon": "Mon",
        "tue": "Tue",
        "wed": "Wed",
        "thu": "Thu",
        "fri": "Fri",
        "sat": "Sat",
        "sun": "Sun",
    }
    if len(ordered) == 1:
        return label_map.get(ordered[0], ordered[0].title())
    if is_contiguous:
        return f"{label_map.get(ordered[0], ordered[0].title())}-{label_map.get(ordered[-1], ordered[-1].title())}"
    return ", ".join(label_map.get(day, day.title()) for day in ordered)


def _format_availability_display(text: str) -> str:
    # Show availability as-entered without timestamp conversions.
    cleaned = _strip_markdown(_normalize_availability_text(text))
    for _, zone_name in TIMEZONE_ENTRIES:
        if zone_name == "UTC":
            cleaned = re.sub(
                r"(?<!\w)UTC(?![+\-\d:])",
                format_timezone_display("UTC"),
                cleaned,
            )
            continue
        cleaned = re.sub(
            rf"(?<!\w){re.escape(zone_name)}(?!\w)",
            format_timezone_display(zone_name),
            cleaned,
        )
    return cleaned


class ExaminationAvailabilityMixin:
    @staticmethod
    def _get_normalized_availability_windows(case: Dict[str, Any]) -> List[Dict[str, Any]]:
        return _normalize_structured_windows(case.get("availability_windows") or [])

    def _update_availability_draft(
        self,
        channel_id: int,
        *,
        days: Optional[Set[str]] = None,
        timezone_text: Optional[str] = None,
        start: Optional[float] = None,
        end: Optional[float] = None,
        user_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        case = self._get_case(channel_id)
        if not case:
            return None
        draft = case.get("availability_draft") or {}
        if days is not None:
            draft["days"] = _ordered_days(days)
        if timezone_text is not None:
            draft["timezone"] = timezone_text
        if start is not None:
            draft["start"] = start
        if end is not None:
            draft["end"] = end
        if user_id is not None:
            draft["user_id"] = user_id
        case["availability_draft"] = draft
        self._save()
        return draft

    def _add_availability_window(self, channel_id: int, *, user_id: Optional[int] = None) -> bool:
        case = self._get_case(channel_id)
        if not case:
            return False
        draft = case.get("availability_draft") or {}
        days = set(draft.get("days") or [])
        timezone_text = draft.get("timezone")
        start = draft.get("start")
        end = draft.get("end")
        if not days or timezone_text is None or start is None or end is None:
            return False
        windows = list(case.get("availability_windows") or [])
        windows.append(
            {
                "days": _ordered_days(days),
                "start": float(start),
                "end": float(end),
                "timezone": timezone_text,
            }
        )
        case["availability_windows"] = _normalize_structured_windows(windows)
        case["availability_set_at"] = datetime.now(timezone.utc).isoformat()
        if user_id:
            case["availability_set_by"] = user_id
        case["availability_draft"] = {}
        self._save()
        return True

    def _clear_availability_draft(self, channel_id: int, *, user_id: Optional[int] = None) -> bool:
        case = self._get_case(channel_id)
        if not case:
            return False
        draft = case.get("availability_draft") or {}
        if not draft:
            return False
        case["availability_draft"] = {}
        if user_id:
            case["availability_set_by"] = user_id
        self._save()
        return True

    def _remove_availability_window(
        self,
        channel_id: int,
        index: int,
        *,
        user_id: Optional[int] = None,
    ) -> bool:
        case = self._get_case(channel_id)
        if not case:
            return False
        windows = _normalize_structured_windows(list(case.get("availability_windows") or []))
        if index < 0 or index >= len(windows):
            case["availability_windows"] = windows
            self._save()
            return False
        windows.pop(index)
        case["availability_windows"] = _normalize_structured_windows(windows)
        if user_id:
            case["availability_set_by"] = user_id
        case["availability_set_at"] = datetime.now(timezone.utc).isoformat()
        self._save()
        return True

    def _finalize_availability(self, channel_id: int, *, user_id: Optional[int] = None) -> Optional[str]:
        case = self._get_case(channel_id)
        if not case:
            return None
        windows = case.get("availability_windows") or []
        draft = case.get("availability_draft") or {}
        draft_days = set(draft.get("days") or [])
        draft_start = draft.get("start")
        draft_end = draft.get("end")
        draft_tz = draft.get("timezone")
        if draft_days and draft_start is not None and draft_end is not None and draft_tz:
            windows.append(
                {
                    "days": _ordered_days(draft_days),
                    "start": float(draft_start),
                    "end": float(draft_end),
                    "timezone": draft_tz,
                }
            )
            case["availability_draft"] = {}
        windows = _normalize_structured_windows(windows)
        if not windows:
            return None
        parts: List[str] = []
        for window in windows:
            days = set(window.get("days") or [])
            start = window.get("start")
            end = window.get("end")
            timezone_text = window.get("timezone") or "UTC"
            if not days or start is None or end is None:
                continue
            parts.append(_build_availability_string(days, float(start), float(end), timezone_text))
        if not parts:
            return None
        availability = " | ".join(parts)
        case["availability"] = availability
        case["availability_structured"] = windows
        case["availability_windows"] = windows
        case["availability_set_at"] = datetime.now(timezone.utc).isoformat()
        if user_id:
            case["availability_set_by"] = user_id
        self._save()
        return availability

    def _build_availability_prompt_embed(self, case: Dict[str, Any], opener: Optional[discord.Member]) -> discord.Embed:
        # Prompt the applicant to set availability using structured inputs.
        embed = discord.Embed(
            title="Set Availability",
            description=(
                "Add one or more times when you're available during the coming week. "
                "Choose your timezone, days, start time, and end time, then click "
                "**Add Window**. Repeat as needed and click **Finish** when you're done.\n\n"
                "Overnight times are supported. An end time earlier than the start "
                "time is treated as the following day."
            ),
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        if opener:
            embed.add_field(name="Applicant", value=opener.mention, inline=False)
        windows = _normalize_structured_windows(case.get("availability_windows") or [])
        saved_windows = _format_structured_availability_display(windows, indexed=True)
        embed.add_field(name="Saved Windows", value=saved_windows or "No availability added yet", inline=False)
        draft = case.get("availability_draft") or {}
        if not draft.get("timezone"):
            default_tz = None
            if windows:
                default_tz = windows[-1].get("timezone")
            if default_tz:
                draft["timezone"] = default_tz
                case["availability_draft"] = draft
                self._save()
        draft_days = set(draft.get("days") or [])
        draft_start = draft.get("start")
        draft_end = draft.get("end")
        draft_tz = draft.get("timezone")
        draft_parts: List[str] = []
        if draft_days:
            draft_parts.append(_format_days(draft_days))
        if draft_start is not None and draft_end is not None:
            draft_parts.append(_format_time_range(float(draft_start), float(draft_end)))
        if draft_tz:
            draft_parts.append(format_timezone_display(str(draft_tz)))
        draft_text = " ".join(draft_parts).strip() if draft_parts else "Not set"
        embed.add_field(name="Current Selection", value=draft_text, inline=False)
        return embed

    def _build_availability_confirm_embed(self, case: Dict[str, Any], opener: Optional[discord.Member]) -> discord.Embed:
        # Confirm the final availability in normalized recurring format.
        embed = discord.Embed(
            title="Availability Saved",
            description="Thanks, your availability has been saved.",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        if opener:
            embed.add_field(name="Applicant", value=opener.mention, inline=False)
        windows = case.get("availability_structured") or []
        availability = case.get("availability") or ""
        availability_text = _format_structured_availability_display(windows)
        if not availability_text:
            availability_text = _format_availability_display(availability)
        if not availability_text:
            availability_text = "Not provided"
        embed.add_field(name="Availability", value=availability_text, inline=False)
        return embed

    async def _ensure_availability_prompt(
        self,
        channel: discord.TextChannel,
        case: Dict[str, Any],
        opener: Optional[discord.Member],
    ) -> None:
        # Post or update the availability prompt in the ticket channel.
        if channel.id in self._availability_prompt_inflight:
            return
        self._availability_prompt_inflight.add(channel.id)
        try:
            from .view import AvailabilityPromptView

            view = AvailabilityPromptView(self, channel.id)
            embed = self._build_availability_prompt_embed(case, opener)
            prompt_id = case.get("availability_prompt_id")
            if prompt_id:
                try:
                    msg = await channel.fetch_message(prompt_id)
                    await msg.edit(embed=embed, view=view)
                    return
                except (discord.NotFound, discord.Forbidden):
                    pass
                except discord.HTTPException:
                    self.logger.exception("Availability prompt update failed: channel_id=%s", channel.id)
            try:
                msg = await channel.send(embed=embed, view=view)
            except (discord.Forbidden, discord.HTTPException):
                self.logger.exception("Availability prompt send failed: channel_id=%s", channel.id)
                return
            case["availability_prompt_id"] = msg.id
            self._save()
        finally:
            self._availability_prompt_inflight.discard(channel.id)

    async def _update_availability_prompt(self, channel_id: int) -> None:
        case = self._get_case(channel_id)
        if not case:
            return
        prompt_id = case.get("availability_prompt_id")
        if not prompt_id:
            return
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        opener = None
        opener_id = case.get("opener_id")
        if opener_id and channel.guild:
            opener = channel.guild.get_member(opener_id)
        embed = self._build_availability_prompt_embed(case, opener)
        from .view import AvailabilityPromptView

        view = AvailabilityPromptView(self, channel_id)
        try:
            msg = await channel.fetch_message(prompt_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        try:
            await msg.edit(embed=embed, view=view)
        except (discord.Forbidden, discord.HTTPException):
            return

    async def _finalize_availability_prompt(self, channel_id: int) -> None:
        # Replace the prompt with a confirmation embed once availability is set.
        case = self._get_case(channel_id)
        if not case:
            return
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        opener = None
        opener_id = case.get("opener_id")
        if opener_id and channel.guild:
            opener = channel.guild.get_member(opener_id)
        embed = self._build_availability_confirm_embed(case, opener)
        prompt_id = case.get("availability_prompt_id")
        if prompt_id:
            try:
                msg = await channel.fetch_message(prompt_id)
                await msg.edit(embed=embed, view=None)
                return
            except (discord.NotFound, discord.Forbidden):
                pass
            except discord.HTTPException:
                self.logger.exception("Availability prompt finalize failed: channel_id=%s", channel_id)
        msg = await channel.send(embed=embed)
        case["availability_prompt_id"] = msg.id
        self._save()

    async def _clear_availability_prompt(self, channel_id: int) -> None:
        case = self._get_case(channel_id)
        if not case:
            return
        prompt_id = case.get("availability_prompt_id")
        case["availability_prompt_id"] = None
        case["availability_draft"] = {}
        self._save()
        if not prompt_id:
            return
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            msg = await channel.fetch_message(prompt_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        try:
            await msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    async def _maybe_route_after_availability(self, channel_id: int) -> None:
        case = self._get_case(channel_id)
        if not case or case.get("routing_message_id") or case.get("routing_inflight"):
            return
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        ticket_type = case.get("type") or "clan_promo"
        await self.route_ticket(channel, ticket_type)
