"""Keep model-facing lookup results as data and short flags."""

from __future__ import annotations

import re
from functools import wraps
from collections.abc import Mapping
from typing import Any


LIMIT_FIELDS = frozenset({
    "limitations", "caveats", "warnings", "projection_note",
    "coverage_note", "interpretation_notes", "caveat",
})


TABLE_MINIMUM_ROWS = 10


def is_record_table(value: Any) -> bool:
    """Identify the homogeneous record lists shown as columns and rows to the model."""
    return (
        isinstance(value, list) and len(value) >= TABLE_MINIMUM_ROWS
        and all(isinstance(item, Mapping) for item in value)
        and all(item.keys() == value[0].keys() for item in value)
    )


def _flag(value: Any) -> str:
    text = str(value).casefold()
    if re.fullmatch(r"[a-z0-9_]+", text):
        return text
    if "not configured" in text or "not set up" in text:
        return "missing_setup"
    if "expired" in text:
        return "expired"
    if "timed out" in text:
        return "timeout"
    if "named source" in text or "other sources" in text or "filter this read" in text:
        return "source_out_of_scope"
    if "time window" in text or "period" in text and "outside" in text:
        return "period_out_of_scope"
    if "argument" in text or "schema" in text:
        return "invalid_arguments"
    if "too large" in text or "limit" in text:
        return "limit"
    if "incomplete" in text or "could not be read completely" in text:
        return "partial"
    if "not available" in text or "unavailable" in text:
        return "unavailable"
    if "invalid" in text or "valid" in text or "required" in text:
        return "invalid_input"
    if "missing" in text or "not found" in text or text.startswith("no "):
        return "empty"
    words = re.findall(r"[a-z0-9]+", text)
    return "_".join(words[:4]) or "failed"


def model_result(
    payload: Mapping[str, Any], *,
    truncated: bool = False,
) -> dict[str, Any]:
    data = dict(payload)
    member_limits = coverage_limits(data)
    if member_limits:
        data["member_limits"] = member_limits
    existing = data.pop("flags", {})
    if not isinstance(existing, Mapping):
        data["data_flags"] = existing
        existing = {}
    limits = list(existing.get("limits", ()))
    for field in LIMIT_FIELDS:
        value = data.pop(field, None)
        if isinstance(value, Mapping):
            data[field] = dict(value)
            continue
        if value:
            items = value if isinstance(value, (list, tuple)) else [value]
            # Retain structured explanations; short flags alone lose their meaning.
            data[field] = value
            limits.extend(_flag(item) for item in items if not isinstance(item, Mapping))
    error = data.get("error")
    if error is not None:
        limits.append(_flag(error))
    truncated = truncated or data.get("truncated") is True or existing.get("truncated") is True
    complete = not truncated and error is None and existing.get("status") not in ("failed", "partial", "refused") and all(
        data.get(field) is not False for field in (
            "complete", "count_complete", "selection_complete", "coverage_complete", "search_complete", "search_is_exhaustive",
        )
    ) and data.get("status") != "partial" and all(
        data.get(field) is None for field in ("next_offset", "next_cursor")
    )
    flags: dict[str, Any] = {**existing,
        "status": "complete" if complete else "partial",
        "truncated": truncated,
    }
    if error is not None or existing.get("status") == "failed":
        flags["status"] = "failed"
    if limits:
        flags["limits"] = sorted(set(limits))
    data["flags"] = flags
    return data


def coverage_limits(data):
    """Expose dates and missing evidence instead of collection mechanics."""
    limits = []
    coverage = data.get("history_coverage")
    if isinstance(coverage, Mapping):
        limits.append("No ranked battle history is available for this period."
                      if coverage.get("status") == "empty" else
                      "Some ranked battles may be missing from this period.")
        for row in data.get("accounts", ()):
            if not isinstance(row, Mapping):
                continue
            dates = [day["date"] for day in row.get("daily", ())
                     if isinstance(day, Mapping) and isinstance(day.get("date"), str)]
            if dates:
                limits.append({"player_tag": row.get("player_tag"),
                               "available_battles_from": min(dates),
                               "available_battles_through": max(dates)})
    for item in data.get("items", ()) if isinstance(data.get("items"), list) else ():
        coverage = item.get("coverage", {}) if isinstance(item, Mapping) else {}
        if not isinstance(coverage, Mapping):
            continue
        available = coverage.get("available_range", {})
        if not isinstance(available, Mapping):
            continue
        if available.get("oldest") and available.get("newest"):
            limits.append({"player_tag": item.get("tag"),
                           "history_from": available["oldest"],
                           "history_through": available["newest"],
                           "message": "Earlier battles are not available here."})
    return limits


def result_handler(handler):
    """Normalize every registered lookup at the model boundary."""
    @wraps(handler)
    async def wrapped(context, arguments):
        return model_result(await handler(context, arguments))
    return wrapped
