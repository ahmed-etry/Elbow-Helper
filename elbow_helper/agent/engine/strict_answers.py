"""Read one count from a complete tabular result for numeric conflict checks."""
from .grounding import GroundingError
from .answer_evidence import observations


COUNT_FIELDS = ("count", "member_count", "player_count", "account_count", "total")


def _rows(value):
    if isinstance(value, dict):
        columns, rows = value.get("columns"), value.get("rows")
        if (not isinstance(columns, list) or not all(isinstance(key, str) for key in columns)
                or len(set(columns)) != len(columns) or not isinstance(rows, list)
                or any(not isinstance(row, list) or len(row) != len(columns) for row in rows)):
            raise GroundingError("invalid_strict_table")
        value = [dict(zip(columns, row)) for row in rows]
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise GroundingError("invalid_strict_rows")
    return value


def _complete(value):
    flags = value.get("flags", {})
    if (not isinstance(flags, dict) or flags.get("status") in ("partial", "failed", "refused")
            or flags.get("truncated") or value.get("truncated") or value.get("error")
            or value.get("status") in ("partial", "unavailable", "not_found", "private", "maintenance")
            or any(value.get(key) is False for key in (
                "complete", "complete_snapshot", "coverage_complete", "search_complete", "search_is_exhaustive"))
            or value.get("next_offset") is not None or value.get("next_cursor") is not None):
        raise GroundingError("incomplete_strict_source")


def _field(rows):
    if not rows:
        return []
    fields = [key for key in COUNT_FIELDS if all(key in row for row in rows)]
    if len(fields) != 1:
        raise GroundingError("ambiguous_strict_field")
    return [row[fields[0]] for row in rows]


def _values(result):
    if "rows" in result and "row_count" in result:
        _complete(result)
        rows = _rows(result["rows"])
        if type(result["row_count"]) is not int or result["row_count"] != len(rows):
            raise GroundingError("invalid_strict_row_count")
        if rows and not any(all(key in row for row in rows) for key in COUNT_FIELDS):
            return None
        return _field(rows)
    return None


def strict_answer(evidence):
    """Return the single nonnegative integer count from one complete query."""
    candidates = [values for result in observations(evidence)
                  for values in [_values(result)] if values is not None]
    if len(candidates) != 1:
        raise GroundingError("strict_source_missing_or_ambiguous")
    values = candidates[0]
    if len(values) != 1 or type(values[0]) is not int or values[0] < 0:
        raise GroundingError("invalid_strict_count")
    return str(values[0])
