"""Stable planning interface generated from registered capabilities."""

from __future__ import annotations

from collections.abc import Mapping

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..models import AgentCapabilityEffect, RegisteredAgentTool
from ..prompts import (
    ACTION_SYSTEM_PROMPT, SYSTEM_PROMPT, PLANNING_RULES,
    ACTION_PLANNING_RULES, STANDING_RULE_RULES,
)


PLAN_TOOL_NAME = "submit_request_plan"


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
        fields = detail.get("properties", {})
        kind = "{" + ",".join(
            f"{name}:{_argument(field)}" for name, field in fields.items()
        ) + "}"
    if "enum" in detail:
        choices = detail["enum"]
        kind += (
            "=" + "/".join(map(str, choices)) if len(choices) <= 20
            else f"({len(choices)} choices)"
        )
    return kind + (
        f"(maxItems={detail["maxItems"]})" if kind.startswith("[") and "maxItems" in detail else ""
    )


def output_forms(registry: Mapping[str, RegisteredAgentTool]) -> tuple[str, ...]:
    return (
        "text",
        *(name for name, tool in registry.items() if tool.effect is AgentCapabilityEffect.ARTIFACT),
    )


def capability_list(registry: Mapping[str, RegisteredAgentTool]) -> str:
    """Return the same compact catalogue for every request."""
    entries = []
    for name in sorted(registry):
        tool = registry[name]
        schema = tool.definition.parameters
        required = set(schema.get("required", ())) | set(schema.get("x-command-required", ()))
        arguments = [
            f"{field}:{_argument(detail)}{'*' if field in required else ''}"
            for field, detail in schema.get("properties", {}).items()
        ]
        meaning = " ".join(tool.definition.description.split(".", 1)[0].split())[:100]
        line = f"{name}: {meaning} | {tool.action_class.value} | args " + ",".join(arguments)
        if tool.returns:
            line += " | returns " + tool.returns
        entries.append(line)
    return "\n".join(entries)


def plan_definition(registry: Mapping[str, RegisteredAgentTool]) -> AgentToolDefinition:
    return AgentToolDefinition(
        name=PLAN_TOOL_NAME,
        description="Submit the goal, scope and ordered capability steps needed to answer.",
        parameters={
            "type": "object",
            "properties": {
                "goal": {"type": "string", "minLength": 1, "maxLength": 240},
                "effort": {"type": "string", "enum": ["low", "high", "max"]},
                "output": {"type": "string", "enum": list(output_forms(registry))},
                "steps": {
                    "type": "array", "minItems": 1, "maxItems": 48,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "minLength": 1, "maxLength": 40},
                            "capability": {"type": "string", "enum": sorted(registry)},
                            "arguments": {"type": "object"},
                            "depends_on": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["id", "capability", "arguments", "depends_on"],
                    },
                },
            },
            "required": ["goal", "effort", "output", "steps"],
            "additionalProperties": False,
        },
    )
