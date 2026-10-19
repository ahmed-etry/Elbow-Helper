"""Bare values from supported complete tool results, without model-authored text."""

import re

from elbow_helper.domain.player_tags import find_player_tags, normalize_player_tag
from ..wording import ACTION_EMPTY
from .grounding import GroundingError


FIELDS = {
    "tags": ("tag", "player_tag", "account_tag"),
    "names": ("name", "player_name", "account_name", "member_name", "display_name"),
    "count": ("count", "member_count", "player_count", "account_count", "total"),
}


def strict_mode(question):
    modes = {mode for mode in FIELDS if re.search(
        rf"\b(?:{mode}[\s-]+only|(?:only|just)\s+(?:(?:the|a)\s+)?{mode})\b", question, re.I,
    )}
    if len(modes) > 1:
        raise GroundingError("conflicting_strict_formats")
    return next(iter(modes), None)


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


def _field(rows, mode):
    if not rows:
        return []
    fields = [key for key in FIELDS[mode] if all(key in row for row in rows)]
    if len(fields) != 1:
        raise GroundingError("ambiguous_strict_field")
    return [row[fields[0]] for row in rows]


def _values(result, mode, question):
    if result.get("source") == "clash_api" and result.get("kind") in ("clan", "player"):
        _complete(result)
        items = _rows(result.get("items"))
        if not items or any(item.get("status") != "ok" for item in items):
            raise GroundingError("strict_accounts_unavailable")
        for item in items:
            _complete(item)
            if not isinstance(item.get("data"), dict):
                raise GroundingError("invalid_strict_account")
        tags = find_player_tags(question)
        if tags and tags != {item.get("tag") for item in items}:
            raise GroundingError("strict_source_wrong_accounts")
        if result["kind"] == "player":
            if mode == "count":
                return None
            return ([item["tag"] for item in items] if mode == "tags"
                    else _field([item["data"] for item in items], mode))
        if len(items) != 1:
            raise GroundingError("ambiguous_strict_clan")
        data = items[0]["data"]
        if mode == "count":
            return [data.get("members")]
        rows = _rows(data.get("memberList"))
        if type(data.get("members")) is not int or len(rows) != data["members"]:
            raise GroundingError("incomplete_strict_roster")
        return _field(rows, mode)
    if "rows" in result and "row_count" in result:
        _complete(result)
        rows = _rows(result["rows"])
        if type(result["row_count"]) is not int or result["row_count"] != len(rows):
            raise GroundingError("invalid_strict_row_count")
        if rows and not any(all(key in row for row in rows) for key in FIELDS[mode]):
            return None
        return _field(rows, mode)
    return None


def strict_answer(question, evidence):
    """Support Clash player batches, one clan roster/count, or one tabular query."""
    mode = strict_mode(question)
    if mode is None:
        return None
    candidates = [values for result in evidence.values() if isinstance(result, dict)
                  for values in [_values(result, mode, question)] if values is not None]
    if len(candidates) != 1:
        raise GroundingError("strict_source_missing_or_ambiguous")
    values = candidates[0]
    if mode == "count":
        if len(values) != 1 or type(values[0]) is not int or values[0] < 0:
            raise GroundingError("invalid_strict_count")
        return str(values[0])
    if not values:
        return ACTION_EMPTY
    if mode == "tags":
        if any(not isinstance(value, str) for value in values):
            raise GroundingError("invalid_strict_tag")
        normalized = [normalize_player_tag(value) for value in values]
        if None in normalized:
            raise GroundingError("invalid_strict_tag")
        values = normalized
    elif any(not isinstance(value, str) or not value.strip() or len(value) > 100
             or any(ord(char) < 32 or ord(char) == 127 for char in value) for value in values):
        raise GroundingError("invalid_strict_name")
    return "\n".join(values)
