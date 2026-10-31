"""Input rules a plan check can decide before any read runs.

A schema lists them under `x-input-rules`; each rule is one dict:
`when` (field -> allowed values, None meaning absent) and `with` (fields that
must all be given) limit when it applies, `unless` (fields) skips it, and one
of `needs` (at least one given), `needs_all`, `only_one`, `forbids`, `within`
(field -> allowed values) or `aware` (dates that must carry a timezone) states
it. `error` is the short planner-facing text; `{values}` names rejected values.
A refusal also names the rule's fields, as a handler's `invalid_fields` would.
Every top-level `format: date-time` field must parse, and each `*after` must be
earlier than its `*before`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

RULES = "x-input-rules"


def _given(arguments: Mapping[str, Any], field: str) -> bool:
    return arguments.get(field) not in (None, False, "", [], {})


def _reference(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"step", "path"}


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _applies(rule: Mapping[str, Any], arguments: Mapping[str, Any]) -> bool:
    if any(_given(arguments, field) for field in rule.get("unless", ())):
        return False
    if not all(_given(arguments, field) for field in rule.get("with", ())):
        return False
    for field, allowed in rule.get("when", {}).items():
        value = arguments.get(field) if _given(arguments, field) else None
        if _reference(value) or value not in allowed:
            return False
    return True


def _broken(rule: Mapping[str, Any], arguments: Mapping[str, Any]) -> str | None:
    """Return the rejected values (possibly empty) when the rule fails, else None."""
    if "needs" in rule and not any(_given(arguments, field) for field in rule["needs"]):
        return ""
    if "needs_all" in rule and not all(_given(arguments, field) for field in rule["needs_all"]):
        return ""
    if "only_one" in rule and sum(_given(arguments, field) for field in rule["only_one"]) > 1:
        return ""
    if "forbids" in rule and any(_given(arguments, field) for field in rule["forbids"]):
        return ""
    for field, allowed in rule.get("within", {}).items():
        values = arguments.get(field)
        if isinstance(values, list):
            rejected = [value for value in values if not _reference(value) and value not in allowed]
            if rejected:
                return ", ".join(map(str, rejected))
    for field in rule.get("aware", ()):
        value = arguments.get(field)
        if isinstance(value, str) and value and _date(value).utcoffset() is None:
            return ""
    return None


def _date_issue(arguments: Mapping[str, Any], schema: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    properties = schema.get("properties", {})
    fields = [field for field, detail in properties.items()
              if isinstance(detail, Mapping) and detail.get("format") == "date-time"
              and isinstance(arguments.get(field), str)]
    parsed = {}
    for field in fields:
        try:
            parsed[field] = _date(arguments[field])
        except ValueError:
            return f"Use an ISO 8601 date for {field}.", (field,)
    for field, start in parsed.items():
        closing = field[:-len("after")] + "before" if field.endswith("after") else None
        if closing in parsed:
            first, last = (value if value.utcoffset() is not None else value.replace(tzinfo=timezone.utc)
                           for value in (start, parsed[closing]))
            if first >= last:
                return "Choose a complete date window with its end after its start.", (field, closing)
    return "", ()


def _fields(rule: Mapping[str, Any]) -> tuple[str, ...]:
    named = [*rule.get("needs", ()), *rule.get("needs_all", ()), *rule.get("only_one", ()),
             *rule.get("forbids", ()), *rule.get("within", {}), *rule.get("aware", ())]
    return tuple(dict.fromkeys(named))


def without_rules(schema: Mapping[str, Any]) -> dict[str, Any]:
    """The schema a refusal shows the planner; its error already names the broken rule."""
    return {key: value for key, value in schema.items() if key != RULES}


def input_rule_issue(arguments: Mapping[str, Any], schema: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    """The first rule these arguments break, as planner-facing text and the fields it names."""
    issue = _date_issue(arguments, schema)
    if issue[0]:
        return issue
    for rule in schema.get(RULES, ()):
        if not _applies(rule, arguments):
            continue
        try:
            rejected = _broken(rule, arguments)
        except ValueError:
            # A date that does not parse is reported by its own handler check.
            continue
        if rejected is not None:
            return rule["error"].replace("{values}", rejected), _fields(rule)
    return "", ()
