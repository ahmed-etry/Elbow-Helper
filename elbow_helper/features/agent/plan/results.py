"""Keep model-facing lookup results as data and short flags."""

from __future__ import annotations

import re
import json
from functools import wraps
from collections.abc import Mapping
from typing import Any
from ..prompts import (
    PLAN_CORRECTION_INSTRUCTION,
)


LIMIT_FIELDS = frozenset({
    "limitations", "caveats", "warnings", "projection_note",
    "coverage_note", "interpretation_notes", "caveat",
})


PAGING_FIELDS = frozenset({
    "report_id", "offset", "limit", "cursor", "next_offset", "next_cursor", "flags",
})


def is_record_table(value: Any) -> bool:
    """Identify the homogeneous record lists shown as columns and rows to the model."""
    return (
        isinstance(value, list) and len(value) >= 10
        and all(isinstance(item, Mapping) for item in value)
        and all(item.keys() == value[0].keys() for item in value)
    )


def compact_result(value: Any) -> Any:
    """Encode homogeneous records as a table only in the model's JSON view."""
    if isinstance(value, Mapping):
        return {key: compact_result(item) for key, item in value.items()}
    if isinstance(value, list):
        if is_record_table(value):
            columns = list(value[0])
            return {"columns": columns, "rows": [[item[column] for column in columns]
                                                  for item in value]}
        return [compact_result(item) for item in value]
    return value


def model_view(
    payload: Mapping[str, Any], *, summaries: dict[str, set[str]] | None = None,
    row_fields=(),
) -> dict[str, Any]:
    """Omit an unchanged retained page summary already included in this request."""
    data = dict(payload)
    if data.get('kind') == 'family':
        from ..families.model_view import family_model_view
        data = family_model_view(data)
    if data.get("kind") == "active_hibernation" and isinstance(data.get("records"), list):
        # The ISO timestamp and status repeat started_ts (null means unavailable).
        # Keep the retained report intact; only its model view needs compact rows.
        data["records"] = [{key: row[key] for key in ("member_id", "started_ts")}
                           for row in data["records"]]
        data["included_fields"] = ["member_id", "started_ts"]
    if "member_limits" in data and isinstance(data.get("accounts"), list):
        data["accounts"] = [{key: value for key, value in row.items()
                             if "poll" not in key} if isinstance(row, Mapping) else row
                            for row in data["accounts"]]
        if isinstance(data.get("history_coverage"), Mapping):
            data["history_coverage"] = {key: value for key, value in data["history_coverage"].items()
                                        if key != "reasons"}
            data["history_coverage"]["message"] = data["member_limits"][0]
        if isinstance(data.get("flags"), Mapping):
            data["flags"] = dict(data["flags"])
            data["flags"]["limits"] = [value for value in data["flags"].get("limits", ())
                                       if "poll" not in value]
    report_id = data.get("report_id")
    paging_fields = PAGING_FIELDS
    if data.get('kind') == 'family':
        paging_fields = paging_fields | {'kind', 'family', 'requester_id', 'selection', 'returned_rows'}
    if (summaries is not None and isinstance(report_id, str) and row_fields
            and ("next_offset" in data or "next_cursor" in data) and "error" not in data):
        summary = {key: value for key, value in data.items()
                   if key not in paging_fields and key not in row_fields}
        signature = json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str,
                               separators=(",", ":"))
        sent = summaries.setdefault(report_id, set())
        if signature in sent:
            data = {key: value for key, value in data.items()
                    if key in paging_fields or key in row_fields}
        sent.add(signature)
    return compact_result(data)


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
    if "not accessible" in text or "not authorized" in text or "permission" in text or "cannot access" in text:
        return "access_denied"
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
            "complete", "complete_snapshot", "count_complete", "selection_complete", "coverage_complete", "search_complete", "search_is_exhaustive",
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
    wrapped.normalizes_results = True
    return wrapped


def plan_feedback(error: str, *, step_id: str = "", offered=()) -> dict[str, Any]:
    return {"flags": {"status": "refused",
                      "rule": re.sub(r"[^a-z0-9]+", "_", error.casefold()).strip("_")},
            "step_id": step_id, "error": error, "offered": list(offered),
            "instruction": PLAN_CORRECTION_INSTRUCTION}
