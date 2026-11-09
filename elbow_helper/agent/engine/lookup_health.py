"""Feedback signals from structured lookup results, never answer prose."""

from collections.abc import Mapping


def lookup_problem(value):
    if isinstance(value, Mapping):
        history = value.get("history_coverage")
        flags = value.get("flags", {})
        flags = flags if isinstance(flags, Mapping) else {}
        # A next page is a navigation handle, not a failed or missing source.
        paging = (any(value.get(key) is not None for key in ("next_offset", "next_cursor", "source_next_cursor"))
                  and not value.get("truncated") and not flags.get("truncated"))
        statuses = (value.get("status"), flags.get("status"))
        if (value.get("error") or value.get("missing_sources") or value.get("gaps")
                or isinstance(history, Mapping) and history.get("status") == "empty"
                or any(status in (
                    "failed", "refused", "not_found", "unavailable",
                    "private", "maintenance", "timeout", "not_tracked", "stale",
                ) for status in statuses)
                or not paging and ("partial" in statuses or value.get("complete") is False)
                or any(value.get(key) is False for key in (
                    "complete_snapshot", "count_complete", "selection_complete",
                    "coverage_complete", "season_complete", "totals_complete", "source_complete",
                ))):
            return True
        return any(lookup_problem(item) for key, item in value.items()
                   if key != "flags" and isinstance(item, (Mapping, list, tuple)))
    if isinstance(value, (list, tuple)):
        return any(lookup_problem(item) for item in value)
    return False
