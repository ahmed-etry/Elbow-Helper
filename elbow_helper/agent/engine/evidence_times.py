"""Format structured evidence timestamps without inspecting source prose."""

from datetime import datetime, timedelta, timezone


def _time_field(key, parent):
    # These are transport/domain field conventions, never words in member text.
    return isinstance(key, str) and (
        key in ("ts", "timestamp", "date")
        or key.endswith(("_ts", "_at", "_utc", "_timestamp", "_time", "_date", "Time"))
        or key in ("after", "before", "oldest", "newest")
        and isinstance(parent, str)
        and (parent.endswith("_range") or parent in ("period", "window"))
    )


def _timestamp(value):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            return value.isoformat()
        instant = value.astimezone(timezone.utc)
    else:
        if not isinstance(value, (int, float, str)):
            return value
        try:
            seconds = float(value)
        except ValueError:
            # Already readable dates and all other strings stay byte-for-byte.
            return value
        except OverflowError:
            return None
        try:
            instant = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
        except (OverflowError, ValueError):
            # An invalid timestamp is unknown, not a number the writer can cite.
            return None
    return instant.replace(tzinfo=None).isoformat(sep=" ") + " UTC"


def readable_evidence_times(value, *, field="", parent=""):
    """Copy records and compact tables, formatting only named timestamp cells.

    IDs, counters, durations, source prose and raw retained evidence stay intact.
    Epoch values use seconds, matching the bot's storage contracts.
    """
    if isinstance(value, dict):
        columns, rows = value.get("columns"), value.get("rows")
        table = (isinstance(columns, list) and all(isinstance(key, str) for key in columns)
                 and isinstance(rows, list)
                 and all(isinstance(row, list) and len(row) == len(columns) for row in rows))
        result = {key: readable_evidence_times(item, field=key, parent=field)
                  for key, item in value.items() if not table or key != "rows"}
        if table:
            result["rows"] = [[readable_evidence_times(cell, field=key, parent=field)
                               for key, cell in zip(columns, row)] for row in rows]
        return result
    if isinstance(value, (list, tuple)):
        return [readable_evidence_times(item, field=field, parent=parent) for item in value]
    return _timestamp(value) if _time_field(field, parent) else value
