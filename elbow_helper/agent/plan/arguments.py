"""Describe rejected fields using the same validator as plan execution."""

from __future__ import annotations

import json


def takes_single_value(schema):
    """Only collapse collections when the schema rules out an array value."""
    if schema.get("x-result-list"):
        return False
    kind = schema.get("type")
    if isinstance(kind, list):
        return bool(kind) and "array" not in kind
    if kind is not None:
        return kind != "array"
    choices = schema.get("anyOf", ())
    return bool(choices) and all(takes_single_value(choice) for choice in choices)


def schema_variant(base, choice):
    """Inherit typed properties when an object alternative only adds constraints."""
    result = {**base, **choice}
    if "required" in base or "required" in choice:
        result["required"] = list(dict.fromkeys((*base.get("required", ()), *choice.get("required", ()))))
    if "properties" in choice:
        properties = dict(base.get("properties", {}))
        for name, detail in choice["properties"].items():
            properties[name] = schema_variant(properties.get(name, {}), detail)
        result["properties"] = properties
    return result


def argument_errors(arguments, schema, dependencies, valid_value):
    properties = schema.get("properties", {})
    issues = []
    missing = set(schema.get("required", ())) - set(arguments)
    unknown = set(arguments) - set(properties)
    if missing:
        issues.append("Missing fields: " + ", ".join(sorted(missing)))
    alternatives = [set(choice.get("required", ())) for choice in schema.get("anyOf", ())]
    if alternatives and not any(fields <= arguments.keys() for fields in alternatives):
        issues.append("Supply one of: " + " or ".join(", ".join(sorted(fields)) for fields in alternatives))
    if unknown and schema.get("additionalProperties") is not True:
        issues.append("Unsupported fields: " + ", ".join(sorted(unknown)))
    for field, value in arguments.items():
        if field not in properties or valid_value(value, properties[field], dependencies):
            continue
        expected = {key: item for key, item in properties[field].items()
                    if key not in {"description", "default"}}
        issues.append(field + " must match " + json.dumps(expected, separators=(",", ":")))
    return issues
