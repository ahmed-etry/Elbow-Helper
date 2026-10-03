"""Check a scheduled action against the scope its member confirmed."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import re
from typing import Any

from ..actions.contracts import ActionRefused, PreparedAction


VARIABLE_WORDS = frozenset({"target", "member", "player", "account", "content", "message", "text", "body"})
RAW_DISCORD_ID = re.compile(r"(?<!\d)\d{17,20}(?!\d)")


def has_raw_id(text: str) -> bool:
    rendered = re.sub(r"<[@#][!&]?\d+>|https://(?:canary\.|ptb\.)?discord\.com/channels/\d+/\d+(?:/\d+)?",
                      "", text)
    return RAW_DISCORD_ID.search(rendered) is not None


def validate_scope(actions: Sequence[Mapping[str, Any]]) -> None:
    seen: set[str] = set()
    for entry in actions:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("capability"), str):
            raise ActionRefused("Choose an action and its fixed values.")
        fixed = entry.get("fixed_values")
        variable = entry.get("variable_fields")
        maximum = entry.get("max_targets")
        scope_text = entry.get("scope_text")
        if (not isinstance(fixed, Mapping) or not isinstance(variable, list)
                or type(maximum) is not int or not 1 <= maximum <= 100
                or not isinstance(scope_text, str) or not scope_text.strip()):
            raise ActionRefused("Choose fixed values, changing fields and a target limit.")
        if has_raw_id(scope_text):
            raise ActionRefused("Describe the action with names instead of IDs.")
        key = json.dumps((entry["capability"], fixed), sort_keys=True, default=str)
        if key in seen:
            raise ActionRefused("Choose each allowed action once.")
        seen.add(key)
        for name in variable:
            if not isinstance(name, str):
                raise ActionRefused("Only targets and message content may change between runs.")
            words = {word.removesuffix("s") for word in name.lower().split("_")}
            if name in ("ping_everyone", "ping_role_ids") or not words & VARIABLE_WORDS:
                raise ActionRefused("Only targets and message content may change between runs.")
            if name in fixed:
                raise ActionRefused("A field cannot be fixed and changing.")


def within_scope(proposals: Sequence[PreparedAction],
                 allowed: Sequence[Mapping[str, Any]]) -> bool:
    if not proposals and not allowed:
        return True
    counts: dict[int, int] = {}
    steps: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for action in proposals:
        if not action.step_id or not action.capability_name:
            return False
        values = action.checked_arguments
        checked = (action.capability_name, values)
        if action.step_id in steps:
            if not _same_value(steps[action.step_id], checked):
                return False
            continue
        steps[action.step_id] = checked
        match_index = next((index for index, entry in enumerate(allowed)
                            if entry["capability"] == action.capability_name
                            and _matches(values, entry)), None)
        if match_index is None:
            return False
        match = allowed[match_index]
        counts[match_index] = counts.get(match_index, 0) + _targets(values, match)
        if counts[match_index] > match["max_targets"]:
            return False
    return True


def _matches(values: Mapping[str, Any], entry: Mapping[str, Any]) -> bool:
    fixed = entry["fixed_values"]
    variable = set(entry["variable_fields"])
    if variable & {"ping_everyone", "ping_role_ids"}:
        return False
    if set(values) - set(fixed) - variable:
        return False
    return all(name in values and _same_value(values[name], expected)
               for name, expected in fixed.items())


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return left.keys() == right.keys() and all(_same_value(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)) and isinstance(right, type(left)):
        return len(left) == len(right) and all(_same_value(a, b) for a, b in zip(left, right))
    return type(left) is type(right) and left == right


def _targets(values: Mapping[str, Any], entry: Mapping[str, Any]) -> int:
    count = 1
    for name in entry["variable_fields"]:
        value = values.get(name)
        words = {word.removesuffix("s") for word in name.lower().split("_")}
        if not words & {"target", "member", "player", "account"}:
            continue
        if isinstance(value, list):
            count = max(count, len(value))
        elif isinstance(value, str):
            count = max(count, len(re.findall(r"<@!?\d+>|[^,\s<>]+", value)))
    return count
