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
    if "expired" in text:
        return "expired"
    if "timed out" in text:
        return "timeout"
    if "not accessible" in text or "not authorized" in text or "permission" in text:
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
    limits = []
    for field in LIMIT_FIELDS:
        value = data.pop(field, None)
        if value:
            items = value if isinstance(value, (list, tuple)) else [value]
            limits.extend(_flag(item) for item in items)
    error = data.pop("error", None)
    if error is not None:
        limits.append(_flag(error))
    truncated = truncated or data.get("truncated") is True
    complete = not truncated and error is None and all(
        data.get(field) is not False for field in ("complete", "complete_snapshot")
    )
    flags: dict[str, Any] = {
        "status": "complete" if complete else "partial",
        "truncated": truncated,
    }
    if error is not None:
        flags["status"] = "failed"
    if limits:
        flags["limits"] = sorted(set(limits))
    if coverage_dates:
        flags["coverage_dates"] = dict(coverage_dates)
    data["flags"] = flags
    return data
