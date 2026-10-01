"""Check a scheduled action against the scope its member confirmed."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..actions.contracts import PreparedAction


VARIABLE_WORDS = frozenset({"target", "member", "player", "account", "content", "message", "text", "body"})


def validate_scope(actions: Sequence[Mapping[str, Any]]) -> None:
    for entry in actions:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("capability"), str):
            raise ValueError("Choose an action and its fixed values.")
        fixed = entry.get("fixed_values")
        variable = entry.get("variable_fields")
        maximum = entry.get("max_targets")
        scope_text = entry.get("scope_text")
        if (not isinstance(fixed, Mapping) or not isinstance(variable, list)
                or type(maximum) is not int or not 1 <= maximum <= 100
                or not isinstance(scope_text, str) or not scope_text.strip()):
            raise ValueError("Choose fixed values, changing fields and a target limit.")
        for name in variable:
            if not isinstance(name, str):
                raise ValueError("Only targets and message content may change between runs.")
            words = {word.removesuffix("s") for word in name.lower().split("_")}
            if not words & VARIABLE_WORDS:
                raise ValueError("Only targets and message content may change between runs.")
            if name in fixed:
                raise ValueError("A field cannot be fixed and changing.")


def within_scope(proposals: Sequence[PreparedAction],
                 allowed: Sequence[Mapping[str, Any]]) -> bool:
    if not proposals and not allowed:
        return True
    counts: dict[str, int] = {}
    for action in proposals:
        match = next((entry for entry in allowed
                      if entry["capability"] == action.path and
                      _matches(action.values, entry)), None)
        if match is None:
            return False
        name = match["capability"]
        counts[name] = counts.get(name, 0) + _targets(action.values, match)
        if counts[name] > match["max_targets"]:
            return False
    return True


def _matches(values: Mapping[str, Any], entry: Mapping[str, Any]) -> bool:
    fixed = entry["fixed_values"]
    variable = set(entry["variable_fields"])
    if set(values) - set(fixed) - variable:
        return False
    return all(values.get(name) == expected for name, expected in fixed.items())


def _targets(values: Mapping[str, Any], entry: Mapping[str, Any]) -> int:
    count = 1
    for name in entry["variable_fields"]:
        value = values.get(name)
        if isinstance(value, list):
            count = max(count, len(value))
    return count
