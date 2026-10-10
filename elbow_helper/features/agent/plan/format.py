"""Stable planning interface generated from registered capabilities."""

from __future__ import annotations

from collections.abc import Mapping

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..models import AgentCapabilityEffect, RegisteredAgentTool
from ..prompts import (
    BEHAVIOR_RULES,
    ACTION_CAPABILITY_PARAGRAPH,
    READ_ONLY_CAPABILITY_PARAGRAPH,
    RESPONSE_RULES,
    DATA_RULES,
    PLANNING_RULES,
    ACTION_PLANNING_RULES,
    STANDING_RULE_RULES,
)


PLAN_TOOL_NAME = "submit_request_plan"


def system_instructions(
    registry: Mapping[str, RegisteredAgentTool],
    *,
    actions_enabled: bool = True,
    data_guide: str = "",
    community_knowledge: str = "",
) -> str:
    data_rules = DATA_RULES if "find_gif" in registry else DATA_RULES.rsplit("\n", 1)[0]
    parts = [
        BEHAVIOR_RULES,
        ACTION_CAPABILITY_PARAGRAPH if actions_enabled else READ_ONLY_CAPABILITY_PARAGRAPH,
        RESPONSE_RULES,
        data_rules,
        PLANNING_RULES,
    ]
    if actions_enabled:
        parts.extend((ACTION_PLANNING_RULES, STANDING_RULE_RULES))
    parts.append(f"<capabilities>\n{capability_list(registry)}\n</capabilities>")
    if data_guide:
        parts.append(f"<data_guide>\n{data_guide}\n</data_guide>")
    if community_knowledge:
        parts.append(f"<community_knowledge>\n{community_knowledge}\n</community_knowledge>")
    return "\n\n".join(parts)


def _argument(detail: Mapping) -> str:
    if detail.get("x-result-list"):
        return "reference(list of records)"
    if "anyOf" in detail and "type" not in detail:
        return "/".join(_argument(choice) for choice in detail["anyOf"])
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
