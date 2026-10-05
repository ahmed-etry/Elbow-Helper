"""Describe rejected fields using the same validator as plan execution."""

from __future__ import annotations

import json


def period_fields_error(kind, period, fields):
    if set(period) == set(fields):
        return ""
    missing, extra = set(fields) - set(period), set(period) - set(fields)
    details = []
    if missing:
        details.append("add " + ", ".join(sorted(missing)))
    if extra:
        details.append("remove " + ", ".join(sorted(extra)))
    return f"Period {kind}: {'; '.join(details)}."


def argument_errors(arguments, schema, dependencies, valid_value):
    properties = schema.get("properties", {})
    issues = []
    missing = set(schema.get("required", ())) - set(arguments)
    unknown = set(arguments) - set(properties)
    if missing:
        issues.append("Missing fields: " + ", ".join(sorted(missing)))
    if unknown:
        issues.append("Unsupported fields: " + ", ".join(sorted(unknown)))
    for field, value in arguments.items():
        if field not in properties or valid_value(value, properties[field], dependencies):
            continue
        expected = {key: item for key, item in properties[field].items()
                    if key not in {"description", "default"}}
        issues.append(field + " must match " + json.dumps(expected, separators=(",", ":")))
    return issues
