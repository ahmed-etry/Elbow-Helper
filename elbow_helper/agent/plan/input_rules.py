"""Input rules a plan check can decide before any read runs.

A schema lists them under `x-input-rules`; each rule is one dict:
`when` (field -> allowed values, None meaning absent) and `with` (fields that
must all be given) limit when it applies, `unless` (fields) skips it, and one
of `needs` (at least one given), `needs_all`, `only_one`, `forbids`, `forbids_sent`, `within`
(field -> allowed values) or `aware` (dates that must carry a timezone) states it.
`error` is the short planner-facing text; `{values}` names rejected values.
`forbids_sent` rejects supplied fields even when their values are empty, unless a value is the field's
schema default; `forbids` checks given values.
`rule_sentences` writes the same rules as sentences for a reader without this grammar.
A refusal distinguishes missing rule inputs from invalid supplied fields.
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


def _sent(arguments: Mapping[str, Any], field: str, schema: Mapping[str, Any]) -> bool:
    """Supplied, and not just the field's schema default repeated."""
    if field not in arguments:
        return False
    default = schema.get("properties", {}).get(field, {}).get("default")
    value = arguments[field]
    return default is None or type(value) is not type(default) or value != default


def _broken(rule: Mapping[str, Any], arguments: Mapping[str, Any], schema: Mapping[str, Any]) -> str | None:
    """Return the rejected values (possibly empty) when the rule fails, else None."""
    if "needs" in rule and not any(_given(arguments, field) for field in rule["needs"]):
        return ""
    if "needs_all" in rule and not all(_given(arguments, field) for field in rule["needs_all"]):
        return ""
    if "only_one" in rule and sum(_given(arguments, field) for field in rule["only_one"]) > 1:
        return ""
    if "forbids" in rule and any(_given(arguments, field) for field in rule["forbids"]):
        return ""
    if "forbids_sent" in rule and any(_sent(arguments, field, schema) for field in rule["forbids_sent"]):
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
             *rule.get("forbids", ()), *rule.get("forbids_sent", ()), *rule.get("within", {}), *rule.get("aware", ())]
    return tuple(dict.fromkeys(named))


def _invalid_fields(rule: Mapping[str, Any], arguments: Mapping[str, Any],
                    schema: Mapping[str, Any]) -> tuple[str, ...]:
    fields = [field for field in rule.get('only_one', ()) if _given(arguments, field)]
    fields.extend(field for field in rule.get('forbids', ()) if _given(arguments, field))
    fields.extend(field for field in rule.get('forbids_sent', ()) if _sent(arguments, field, schema))
    fields.extend(field for field, allowed in rule.get('within', {}).items()
                  if isinstance(arguments.get(field), list) and any(
                      not _reference(value) and value not in allowed for value in arguments[field]))
    fields.extend(field for field in rule.get('aware', ())
                  if isinstance(arguments.get(field), str) and arguments[field]
                  and _date(arguments[field]).utcoffset() is None)
    return tuple(dict.fromkeys(fields))


def _value(value: Any) -> str:
    return "omitted" if value is None else str(value).lower() if isinstance(value, bool) else str(value)


def _choices(values: Any) -> str:
    """Values joined with "or"; runs of whole numbers become ranges."""
    values, words = list(values), []
    numbers = sorted(value for value in values if type(value) is int)
    start = previous = None
    for number in [*numbers, None]:
        if previous is not None and number == previous + 1:
            previous = number
            continue
        if start is not None:
            words.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    words.extend(_value(value) for value in values if type(value) is not int)
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " or " + words[-1]


def _names(fields: Any, joiner: str) -> str:
    fields = list(fields)
    return fields[0] if len(fields) == 1 else ", ".join(fields[:-1]) + f" {joiner} " + fields[-1]


def _requirement(rule: Mapping[str, Any]) -> str:
    kinds = [key for key in ("needs", "needs_all", "only_one", "exactly_one", "forbids", "forbids_sent", "within", "aware") if key in rule]
    if len(kinds) != 1:
        raise ValueError(f"Input rule needs exactly one requirement: {rule}")
    fields = rule[kinds[0]]
    if kinds[0] == "needs":
        return "give " + (fields[0] if len(fields) == 1 else "one of " + _names(fields, "or"))
    if kinds[0] == "needs_all":
        return "give " + _names(fields, "and")
    if kinds[0] == "only_one":
        text = "give at most one of " + _names(fields, "or")
        if rule.get("together"):
            text += ", except " + _names(rule["together"], "and") + " may be given together"
        return text
    if kinds[0] == "exactly_one":
        return "give exactly one of " + _names(fields, "or")
    if kinds[0] in ("forbids", "forbids_sent"):
        return "leave out " + _names(fields, "and")
    if kinds[0] == "within":
        return "; ".join(f"{field} takes only {_choices(values)}" for field, values in fields.items())
    return _names(fields, "and") + (" needs" if len(fields) == 1 else " need") + " a timezone"


def _sentence_rules(rules):
    """Combine equivalent requirements for display only; validation keeps its rules."""
    rules = [dict(rule) for rule in rules]
    for rule in rules:
        if "forbids_sent" in rule:
            rule["forbids"] = rule.pop("forbids_sent")
    removed = set()
    def conditions(rule):
        return {key: value for key, value in rule.items() if key in ("when", "with", "unless")}
    for index, rule in enumerate(rules):
        if index in removed:
            continue
        for other_index in range(index + 1, len(rules)):
            other = rules[other_index]
            if other_index in removed:
                continue
            requirement = {key: value for key, value in rule.items() if key not in ("when", "error")}
            alternative = {key: value for key, value in other.items() if key not in ("when", "error")}
            def unordered(fields):
                return {key: set(value) if isinstance(value, list) else value for key, value in fields.items()}
            if (unordered(requirement) != unordered(alternative)
                    or rule.get("when", {}).keys() != other.get("when", {}).keys()):
                continue
            different = [key for key in rule.get("when", {})
                         if set(rule["when"][key]) != set(other["when"][key])]
            if len(different) <= 1:
                if different:
                    key = different[0]
                    rule["when"] = {**rule["when"], key: list(dict.fromkeys(
                        (*rule["when"][key], *other["when"][key])))}
                removed.add(other_index)
    for index, rule in enumerate(rules):
        if index in removed:
            continue
        for other_index in range(index + 1, len(rules)):
            other = rules[other_index]
            if other_index in removed or conditions(rule) != conditions(other):
                continue
            if ("needs" in rule and "only_one" in other
                    and set(rule["needs"]) == set(other["only_one"])):
                rule["exactly_one"] = rule.pop("needs")
                removed.add(other_index)
            elif ("only_one" in rule and "needs" in other
                    and set(rule["only_one"]) == set(other["needs"])):
                rule["exactly_one"] = rule.pop("only_one")
                removed.add(other_index)
            elif "only_one" in rule and "only_one" in other and "together" not in rule:
                first, second = set(rule["only_one"]), set(other["only_one"])
                if first <= second or second <= first:
                    rule["only_one"] = list(dict.fromkeys((*rule["only_one"], *other["only_one"])))
                    removed.add(other_index)
                elif len(first - second) == len(second - first) == 1:
                    rule["together"] = [*(field for field in rule["only_one"] if field not in second),
                                        *(field for field in other["only_one"] if field not in first)]
                    rule["only_one"] = list(dict.fromkeys((*rule["only_one"], *other["only_one"])))
                    removed.add(other_index)
    return [rule for index, rule in enumerate(rules) if index not in removed]


def rule_sentences(schema: Mapping[str, Any], *, omit_unless: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Each input rule as one plain sentence, for a reader that is not shown the rule grammar.

    `omit_unless` drops exemptions the caller states once for every rule.
    """
    properties = schema.get("properties", {})
    rules = _sentence_rules(schema.get(RULES, ()))
    always = set(schema.get("required", ())) | {
        field for rule in rules if set(rule) <= {"needs_all", "error", "unless"}
        and set(rule.get("unless", ())) <= set(omit_unless) for field in rule["needs_all"]}
    sentences = []
    for rule in rules:
        when = dict(rule.get("when", {}))
        given = list(rule.get("with", ()))
        unless = [field for field in rule.get("unless", ()) if field not in omit_unless]
        forbids = rule.get("forbids", rule.get("forbids_sent", ()))
        if len(forbids) == 1 and forbids[0] in when:
            # A rule that forbids a field when it holds listed values limits that field's values.
            requirement = f"{forbids[0]} can't be {_choices(when.pop(forbids[0]))}"
        else:
            requirement = _requirement(rule)
            if forbids and len(when) == 1 and not given and not unless:
                field, values = next(iter(when.items()))
                options = [value for value in properties.get(field, {}).get("enum", ()) if value not in values]
                if None not in values and field not in always:
                    options.append(None)
                if options and len(options) < len(values):
                    sentences.append(f"{_names(forbids, 'and')} only with {field} {_choices(options)}.")
                    continue
        condition = [f"{field} is {_choices(values)}" for field, values in when.items()]
        if given:
            condition.append(_names(given, "and") + (" is" if len(given) == 1 else " are") + " given")
        sentence = requirement
        if unless:
            sentence = f"unless {_names(unless, 'or')} is given, {sentence}"
        if condition:
            sentence = f"if {' and '.join(condition)}, {sentence}"
        if sentence.startswith(("give ", "leave ", "unless ", "if ")):
            # Field names keep their spelling; only a leading word of the sentence is capitalized.
            sentence = sentence[0].upper() + sentence[1:]
        sentences.append(sentence + ".")
    return tuple(dict.fromkeys(sentences))


def without_rules(schema: Mapping[str, Any]) -> dict[str, Any]:
    """The schema a refusal shows the planner; its error already names the broken rule."""
    return {key: value for key, value in schema.items() if key != RULES}


def input_rule_issue(
    arguments: Mapping[str, Any], schema: Mapping[str, Any],
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Return the first issue's text, invalid fields and missing fields."""
    issue = _date_issue(arguments, schema)
    if issue[0]:
        return issue[0], issue[1], ()
    for rule in schema.get(RULES, ()):
        if not _applies(rule, arguments):
            continue
        try:
            rejected = _broken(rule, arguments, schema)
        except ValueError:
            # A date that does not parse is reported by its own handler check.
            continue
        if rejected is not None:
            message = rule["error"].replace("{values}", rejected)
            if "needs" in rule or "needs_all" in rule:
                return message, (), tuple(field for field in _fields(rule)
                                          if not _given(arguments, field))
            return message, _invalid_fields(rule, arguments, schema), ()
    return "", (), ()
