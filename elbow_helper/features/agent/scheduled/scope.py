"""Check a scheduled action against the scope its member confirmed."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import re
from typing import Any

from ..actions.contracts import PreparedAction


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
            raise ValueError("Choose an action and its fixed values.")
        fixed = entry.get("fixed_values")
        variable = entry.get("variable_fields")
        maximum = entry.get("max_targets")
        scope_text = entry.get("scope_text")
        if (not isinstance(fixed, Mapping) or not isinstance(variable, list)
                or type(maximum) is not int or not 1 <= maximum <= 100
                or not isinstance(scope_text, str) or not scope_text.strip()):
            raise ValueError("Choose fixed values, changing fields and a target limit.")
        if has_raw_id(scope_text):
            raise ValueError("Describe the action with names instead of IDs.")
        key = json.dumps((entry["capability"], fixed), sort_keys=True, default=str)
        if key in seen:
            raise ValueError("Choose each allowed action once.")
        seen.add(key)
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
    counts: dict[int, int] = {}
    for action in proposals:
        match_index = next((index for index, entry in enumerate(allowed)
                            if entry.get("action_path", entry["capability"]) == action.path
                            and _matches(action.values, entry)), None)
        if match_index is None:
            return False
        match = allowed[match_index]
        counts[match_index] = counts.get(match_index, 0) + _targets(action.values, match)
        if counts[match_index] > match["max_targets"]:
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
        elif isinstance(value, str) and {
            word.removesuffix("s") for word in name.lower().split("_")
        } & {"target", "member", "player", "account"}:
            count = max(count, len(re.findall(r"<@!?\d+>|[^,\s<>]+", value)))
    return count
