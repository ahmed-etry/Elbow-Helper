"""Keep model-facing lookup results as data and short flags."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


LIMIT_FIELDS = frozenset({
    "limitations", "caveats", "warnings", "projection_note",
    "coverage_note", "interpretation_notes", "caveat",
})


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
    payload: Mapping[str, Any], *, coverage_dates: Mapping[str, Any] | None = None,
    truncated: bool = False,
) -> dict[str, Any]:
    data = dict(payload)
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
            limits.extend(_flag(item) for item in items)
    error = data.pop("error", None)
    if error is not None:
        limits.append(_flag(error))
    truncated = truncated or data.get("truncated") is True or existing.get("truncated") is True
    complete = not truncated and error is None and existing.get("status") not in ("failed", "partial", "refused") and all(
        data.get(field) is not False for field in ("complete", "complete_snapshot")
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
    if coverage_dates:
        flags["coverage_dates"] = dict(coverage_dates)
    data["flags"] = flags
    return data


def result_handler(handler):
    """Normalize every registered lookup at the model boundary."""
    from functools import wraps
    @wraps(handler)
    async def wrapped(context, arguments):
        return model_result(await handler(context, arguments))
    wrapped.normalizes_results = True
    return wrapped


def plan_feedback(error: str, *, step_id: str = "", offered=()) -> dict[str, Any]:
    return {"flags": {"status": "refused",
                      "rule": re.sub(r"[^a-z0-9]+", "_", error.casefold()).strip("_")},
            "step_id": step_id, "offered": list(offered),
            "instruction": "Correct the plan once or offer the refused sources without reading them."}
