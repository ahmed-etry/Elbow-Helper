"""Describe rejected fields using the same validator as plan execution."""

from __future__ import annotations

import json


def argument_errors(arguments, schema, dependencies, valid_value):
    properties = schema.get("properties", {})
    issues = []
    missing = set(schema.get("required", ())) - set(arguments)
    unknown = set(arguments) - set(properties)
    if missing:
        issues.append("Missing fields: " + ", ".join(sorted(missing)))
    if unknown and schema.get("additionalProperties") is not True:
        issues.append("Unsupported fields: " + ", ".join(sorted(unknown)))
    for field, value in arguments.items():
        if field not in properties or valid_value(value, properties[field], dependencies):
            continue
        expected = {key: item for key, item in properties[field].items()
                    if key not in {"description", "default"}}
        issues.append(field + " must match " + json.dumps(expected, separators=(",", ":")))
    return issues
