"""Stable planning interface generated from registered capabilities."""

from __future__ import annotations

from collections.abc import Mapping
import json

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..models import AgentCapabilityEffect, RegisteredAgentTool
from ..prompts import ACTION_SYSTEM_PROMPT, SYSTEM_PROMPT


PLAN_TOOL_NAME = "submit_request_plan"


def system_instructions(
    registry: Mapping[str, RegisteredAgentTool],
    *,
    actions_enabled: bool = True,
    data_guide: str = "",
    community_knowledge: str = "",
) -> str:
    parts = [ACTION_SYSTEM_PROMPT if actions_enabled else SYSTEM_PROMPT]
    parts.append("<capabilities>\nArguments: trailing * is required; numeric [min..max] is inclusive; "
                 "array {min..max} counts items.\n" + capability_list(registry) + "\n</capabilities>")
    if data_guide:
        parts.append(f"<data_guide>\n{data_guide}\n</data_guide>")
    if community_knowledge:
        parts.append(f"<community_knowledge>\n{community_knowledge}\n</community_knowledge>")
    return "\n\n".join(parts)


def _argument(detail: Mapping) -> str:
    if detail.get("x-result-list"):
        return "reference(list of records)"
    if detail.get("x-field-path"):
        return "string(dotted field)"
    if "anyOf" in detail and "type" not in detail:
        return "/".join(_argument(choice) for choice in detail["anyOf"])
    kind = detail.get("type", "value")
    if isinstance(kind, list):
        kind = "/".join(kind)
    if kind == "array":
        kind = "[" + _argument(detail.get("items", {})) + "]"
    elif kind == "object":
        fields = detail.get("properties", {})
        required = set(detail.get("required", ()))
        kind = "{" + ",".join(
            f"{name}:{_argument(field)}{'*' if name in required else ''}"
            for name, field in fields.items()
        ) + "}"
        if not fields and isinstance(detail.get("additionalProperties"), Mapping):
            kind = "{*:" + _argument(detail["additionalProperties"]) + "}"
    else:
        kind = {"integer": "int", "boolean": "bool", "string": "str"}.get(kind, kind)
    if "enum" in detail:
        choices = detail["enum"]
        kind += (
            "=" + "/".join(map(str, choices)) if len(choices) <= 20
            else f"({len(choices)} choices)"
        )
    if "minimum" in detail and "maximum" in detail:
        kind += f"[{detail['minimum']}..{detail['maximum']}]"
    elif "minimum" in detail:
        kind += f">={detail['minimum']}"
    elif "maximum" in detail:
        kind += f"<={detail['maximum']}"
    if "minItems" in detail and "maxItems" in detail:
        kind += f"{{{detail['minItems']}..{detail['maxItems']}}}"
    elif "minItems" in detail:
        kind += f"{{>={detail['minItems']}}}"
    elif "maxItems" in detail:
        kind += f"{{<={detail['maxItems']}}}"
    if "default" in detail:
        kind += "(default=" + json.dumps(detail["default"], ensure_ascii=False, separators=(',', ':')) + ")"
    return kind


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
        meaning = tool.definition.description
        note = tool.definition.usage_note
        if not meaning or len(meaning) > 100 or "\n" in meaning:
            raise ValueError(
                f"Capability {name} needs a whole one-line summary within 100 characters"
            )
        if len(note) > 160 or "\n" in note:
            raise ValueError(
                f"Capability {name} usage note must fit one line within 160 characters"
            )
        line = f"{name}: {meaning} | {tool.action_class.value} | " + ",".join(arguments)
        if note:
            line += " | use " + note
        if tool.returns:
            line += " | returns " + _catalogue_returns(tool, registry)
        entries.append(line)
    return "\n".join(entries)


def _catalogue_returns(tool, registry):
    """Reference identical record shapes already present in this catalogue."""
    hint = tool.returns
    if tool.definition.name == "read_saved_report":
        from ..engine.result_hints import REPORT_FIELDS
        from ..engine.result_fields import format_returns

        for kind, shape in REPORT_FIELDS.items():
            fields = format_returns(shape)
            original = next((name for name, candidate in sorted(registry.items())
                             if name != "read_saved_report" and candidate.returns == fields), None)
            if original:
                hint = hint.replace(kind + ":{" + fields + "}", kind + ":same as " + original)
    return hint


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
