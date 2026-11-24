"""Check tool argument schemas, input rules and action references."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any
import re
import math

from ..engine.models import RegisteredAgentTool
from ..reports.tools import (READ_NAME, original_arguments, original_tool,
                            unsupported_fields, unsupported_field_error)
from .arguments import argument_errors, schema_variant
from .input_rules import input_rule_issue
from .reference_paths import valid_path


@dataclass(frozen=True, slots=True)
class PlanCheck:
    ok: bool
    error: str = ""
    step_id: str = ""
    offered: tuple[str, ...] = ()
    step_errors: Mapping[str, str] = field(default_factory=dict)
    # Invalid fields named by an input rule, for one step or per refused step.
    fields: tuple[str, ...] = ()
    step_fields: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    missing_fields: tuple[str, ...] = ()
    step_missing_fields: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


def _error(message, step_id="", offered=(), fields=(), missing_fields=()):
    return PlanCheck(False, message, step_id, offered, fields=fields, missing_fields=missing_fields)


def _valid_value(
    value: Any, schema: Mapping[str, Any], dependencies: set[str] | None = None,
) -> bool:
    if dependencies is not None and isinstance(value, dict) and set(value) == {"step", "path"}:
        return _reference(value, dependencies)
    if schema.get("x-result-list"):
        # The handler reports an invalid resolved record shape with its specific error.
        return True
    if "anyOf" in schema and "type" not in schema:
        return any(_valid_value(value, choice, dependencies) for choice in schema["anyOf"])
    if "enum" in schema and value not in schema["enum"]:
        return False
    kind = schema.get("type")
    if isinstance(kind, list):
        return any(_valid_value(value, {**schema, "type": choice}, dependencies) for choice in kind)
    if kind == "null":
        return value is None
    if kind == "string":
        return (isinstance(value, str) and (bool(value.strip()) or schema.get("minLength") == 0)
                and ("pattern" not in schema or re.search(schema["pattern"], value) is not None)
                and schema.get("minLength", 1) <= len(value) <= schema.get("maxLength", 2**31))
    if kind == "integer":
        return (type(value) is int and schema.get("minimum", -(2**63)) <= value
                <= schema.get("maximum", 2**64 - 1))
    if kind == "number":
        return (
            type(value) in (int, float) and math.isfinite(value)
            and schema.get("minimum", float("-inf")) <= value
            <= schema.get("maximum", float("inf"))
        )
    if kind == "boolean":
        return type(value) is bool
    if kind == "array":
        return (isinstance(value, list) and schema.get("minItems", 0) <= len(value)
                <= schema.get("maxItems", 2**31)
                and all(_valid_value(item, schema["items"], dependencies) for item in value)
                and (not schema.get("uniqueItems")
                     or len({str(item) for item in value}) == len(value)))
    if kind == "object":
        return valid_arguments(value, schema, dependencies)
    return False


def valid_arguments(
    arguments: Any, schema: Mapping[str, Any], dependencies: set[str] | None = None,
) -> bool:
    if not isinstance(arguments, dict):
        return False
    if "anyOf" in schema:
        base = {key: value for key, value in schema.items() if key != "anyOf"}
        return valid_arguments(arguments, base, dependencies) and any(
            valid_arguments(arguments, schema_variant(base, choice), dependencies)
            for choice in schema["anyOf"])
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        return False
    additional = schema.get("additionalProperties", False)
    if (
        set(arguments) - set(properties) and additional is not True
        and not isinstance(additional, Mapping)
    ) or set(schema.get("required", ())) - set(arguments):
        return False
    return all(
        _valid_value(value, properties.get(field, additional), dependencies)
        for field, value in arguments.items()
        if field in properties or isinstance(additional, Mapping)
    )


def _reference(value: Any, earlier: set[str]) -> bool:
    return (isinstance(value, dict) and set(value) == {"step", "path"}
            and isinstance(value["step"], str) and value["step"] in earlier
            and valid_path(value["path"]))


def argument_issue(
    registry: Mapping[str, RegisteredAgentTool], name: str, arguments: Any,
    dependencies: set[str] | None = None, *, step_id: str = "",
) -> PlanCheck | None:
    """Check a lookup's schema and rules, including a saved report's own kind."""
    tool = registry[name]
    if not isinstance(arguments, dict):
        return _error("Use an argument object.", step_id)
    selected = original_tool(registry, name, arguments) if name == READ_NAME else None
    if selected is not None and unsupported_fields(selected, arguments):
        return _error(unsupported_field_error(selected, arguments), step_id)
    if not valid_arguments(arguments, tool.definition.parameters, dependencies):
        issues = argument_errors(arguments, tool.definition.parameters, dependencies, _valid_value)
        return _error("Invalid arguments: " + "; ".join(issues), step_id)
    if name == READ_NAME:
        if selected is None:
            return _error("Use the fields supported by this report kind.", step_id)
        inner = original_arguments(arguments)
        if not valid_arguments(inner, selected.definition.parameters, dependencies):
            issues = argument_errors(inner, selected.definition.parameters, dependencies, _valid_value)
            return _error("Invalid arguments: " + "; ".join(issues), step_id)
    issue, fields, missing_fields = input_rule_issue(
        original_arguments(arguments) if selected else arguments,
        (selected or tool).definition.parameters,
    )
    if issue:
        return _error(issue, step_id, fields=fields, missing_fields=missing_fields)
    return None
