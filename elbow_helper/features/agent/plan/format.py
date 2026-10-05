"""Stable planning interface generated from registered capabilities."""

from __future__ import annotations

from collections.abc import Mapping

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..models import AgentCapabilityEffect, RegisteredAgentTool
from ..reports.tools import routed_result_kinds
from ..prompts import (
    ACTION_SYSTEM_PROMPT, SYSTEM_PROMPT, PLANNING_RULES,
    ACTION_PLANNING_RULES, STANDING_RULE_RULES,
)


PLAN_TOOL_NAME = "submit_request_plan"
PERIOD_FIELDS = {
    "utc_range": ("kind", "start", "end"),
    "key": ("kind", "field", "value"),
    "resolved": ("kind", "step", "selector", "path"),
}


def period_schema():
    properties = {
        "start": {"type": "string"}, "end": {"type": "string"},
        "value": {"type": ["string", "integer"]}, "field": {"type": "string"},
        "step": {"type": "string"},
        "path": {"type": "array", "minItems": 1, "maxItems": 8,
                 "items": {"type": ["string", "integer"]}},
        "selector": {"type": "string", "enum": ["latest", "current"]},
    }
    return {"oneOf": [
        {"type": "object", "properties": {
            "kind": {"type": "string", "enum": [kind]},
            **{field: properties[field] for field in fields if field != "kind"},
        }, "required": list(fields), "additionalProperties": False}
        for kind, fields in PERIOD_FIELDS.items()
    ]}


def system_instructions(
    registry: Mapping[str, RegisteredAgentTool], *, actions_enabled: bool = True,
) -> str:
    parts = [ACTION_SYSTEM_PROMPT if actions_enabled else SYSTEM_PROMPT, PLANNING_RULES]
    if actions_enabled:
        parts.extend((ACTION_PLANNING_RULES, STANDING_RULE_RULES))
    parts.append(f"<capabilities>\n{capability_list(registry)}\n</capabilities>")
    return "\n\n".join(parts)


def _argument(detail: Mapping) -> str:
    kind = detail.get("type", "value")
    if isinstance(kind, list):
        kind = "/".join(kind)
    if kind == "array":
        kind = "[" + _argument(detail.get("items", {})) + "]"
    elif kind == "object":
        kind = "{" + ",".join(field + ":" + _argument(value)
                            for field, value in detail.get("properties", {}).items()) + "}"
    if "enum" in detail:
        choices = detail["enum"]
        kind += ("=" + "/".join(map(str, choices)) if len(choices) <= 20
                 else f"({len(choices)} choices)")
    bounds = [f"{key}={detail[key]}" for key in
              ("minimum", "maximum", "minItems", "maxItems", "maxLength") if key in detail]
    if detail.get("minLength") == 0:
        bounds.append("minLength=0")
    return kind + ("(" + ",".join(bounds) + ")" if bounds else "")


def output_forms(registry: Mapping[str, RegisteredAgentTool]) -> tuple[str, ...]:
    return ("text", *(name for name, tool in registry.items()
                      if tool.effect is AgentCapabilityEffect.ARTIFACT))


def capability_list(registry: Mapping[str, RegisteredAgentTool]) -> str:
    """Return the same compact catalogue for every request."""
    entries = []
    for name in sorted(registry):
        tool = registry[name]
        contract = tool.contract
        schema = tool.definition.parameters
        required = set(schema.get("required", ())) | set(schema.get("x-command-required", ()))
        arguments = [
            f"{field}:{_argument(detail)}{'*' if field in required else ''}"
            for field, detail in schema.get("properties", {}).items()
        ]
        if len(arguments) > 16:
            arguments = [
                f"{field}:{_argument(detail)}*"
                for field, detail in schema["properties"].items() if field in required
            ] + [
                "optional " + "/".join(
                    f"{field}:{detail.get('type', 'value')}"
                    for field, detail in schema["properties"].items()
                    if field not in required
                )
            ]
        meaning = " ".join(tool.definition.description.split(".", 1)[0].split())[:120]
        time_fields = ",".join(contract.time_fields) if contract else ""
        if contract and contract.optional_time_window:
            time_fields += " (optional)"
        latest_fields = ",".join(contract.latest_fields) if contract else ""
        period_results = ",".join("/".join(map(str, path)) for path in contract.period_results) if contract else ""
        result_kinds = {**(contract.referenceable_result_kinds if contract else {}),
                        **routed_result_kinds(registry, name)}
        result_paths = ",".join(
            "/".join(path) + (":" + result_kinds[path] if path in result_kinds else "")
            for path in contract.referenceable_result_paths
        ) if contract else ""
        entity_fields = ",".join(
            f"{field}:{kind}" for field, kind in contract.entity_fields
        ) if contract else ""
        patterns = ",".join(f"{field}={pattern}" for field, pattern in contract.value_patterns) if contract else ""
        fields = [f"class {tool.action_class.value}"]
        for label, value in (
            ("args", ",".join(arguments)), ("time", time_fields),
            ("latest", latest_fields), ("periods", period_results),
            ("results", result_paths),
            ("entity", entity_fields), ("formats", patterns),
        ):
            if value:
                fields.append(f"{label} {value}")
        if tool.effect is AgentCapabilityEffect.ARTIFACT:
            fields.append("output")
        entries.append(f"{name}: {meaning} | " + " | ".join(fields))
    return "\n".join(entries)


def plan_definition(registry: Mapping[str, RegisteredAgentTool]) -> AgentToolDefinition:
    return AgentToolDefinition(
        name=PLAN_TOOL_NAME,
        description="Submit the goal, scope and ordered capability steps needed to answer.",
        parameters={
            "type": "object",
            "properties": {
                "goal": {"type": "string", "maxLength": 240},
                "effort": {"type": "string", "enum": ["low", "high", "max"]},
                "output": {"type": "string", "enum": list(output_forms(registry))},
                "periods": {"type": "array", "maxItems": 12, "items": period_schema()},
                "entities": {"type": "array", "maxItems": 32, "items": {
                    "type": "object", "properties": {
                        "kind": {"type": "string"},
                        "value": {"type": ["string", "integer", "object", "array"],
                                  "items": {"type": ["string", "integer", "object"]}},
                    }, "required": ["kind", "value"],
                }},
                "steps": {"type": "array", "minItems": 1, "maxItems": 48,
                          "items": {"type": "object", "properties": {
                              "id": {"type": "string", "maxLength": 40},
                              "capability": {"type": "string", "enum": sorted(registry)},
                              "arguments": {"type": "object"},
                              "reason": {"type": "string", "maxLength": 240},
                              "depends_on": {"type": "array", "items": {"type": "string"}},
                          }, "required": ["id", "capability", "arguments", "reason", "depends_on"]}},
            },
            "required": ["goal", "effort", "output", "steps"],
            "additionalProperties": False,
        },
    )
