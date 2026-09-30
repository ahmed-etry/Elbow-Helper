"""Stable planning interface generated from registered capabilities."""

from __future__ import annotations

from collections.abc import Mapping

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..models import AgentCapabilityEffect, RegisteredAgentTool
from ..capabilities import CONTRACTS


PLAN_TOOL_NAME = "submit_request_plan"


def system_instructions(registry: Mapping[str, RegisteredAgentTool], *, actions_enabled: bool = True) -> str:
    from ..prompts import ACTION_SYSTEM_PROMPT, SYSTEM_PROMPT
    prompt = ACTION_SYSTEM_PROMPT if actions_enabled else SYSTEM_PROMPT
    command_rules = ACTION_PLANNING_INSTRUCTIONS if actions_enabled else ""
    return (prompt + "\n\n" + PLANNING_INSTRUCTIONS + command_rules
            + "\n\n<capabilities>\n" + capability_list(registry) + "\n</capabilities>")


def _argument(detail: Mapping) -> str:
    kind = detail.get("type", "value")
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
    return kind + ("(" + ",".join(bounds) + ")" if bounds else "")


def output_forms(registry: Mapping[str, RegisteredAgentTool]) -> tuple[str, ...]:
    return ("text", *(name for name, tool in registry.items()
                      if tool.effect is AgentCapabilityEffect.ARTIFACT))


def capability_list(registry: Mapping[str, RegisteredAgentTool]) -> str:
    """Return the same compact catalogue for every request."""
    entries = []
    for name in sorted(registry):
        tool = registry[name]
        contract = CONTRACTS.get(name)
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
        latest_fields = ",".join(contract.latest_fields) if contract else ""
        period_results = ",".join("/".join(map(str, path)) for path in contract.period_results) if contract else ""
        entity_fields = ",".join(
            f"{field}:{kind}" for field, kind in contract.entity_fields
        ) if contract else ""
        patterns = ",".join(f"{field}={pattern}" for field, pattern in contract.value_patterns) if contract else ""
        entries.append(
            f"{name}: {meaning} | class {tool.action_class.value} | args {','.join(arguments)} | "
            f"time {time_fields} | latest {latest_fields} | periods {period_results} | entity {entity_fields}"
            + (f" | formats {patterns}" if patterns else "")
            + (" | output" if tool.effect is AgentCapabilityEffect.ARTIFACT else "")
        )
    return "\n".join(entries)


def plan_definition(registry: Mapping[str, RegisteredAgentTool]) -> AgentToolDefinition:
    return AgentToolDefinition(
        name=PLAN_TOOL_NAME,
        description="Submit the goal, scope and ordered capability steps needed to answer.",
        parameters={
            "type": "object",
            "properties": {
                "goal": {"type": "string", "maxLength": 240},
                "effort": {"type": "string", "enum": ["low", "high"]},
                "output": {"type": "string", "enum": list(output_forms(registry))},
                "periods": {"type": "array", "maxItems": 12, "items": {
                    "type": "object", "properties": {
                        "kind": {"type": "string", "enum": ["utc_range", "key", "resolved"]},
                        "start": {"type": "string"}, "end": {"type": "string"},
                        "value": {"type": ["string", "integer"]},
                        "field": {"type": "string"},
                        "step": {"type": "string"},
                        "path": {"type": "array", "items": {"type": ["string", "integer"]}},
                        "selector": {"type": "string", "enum": ["latest", "current"]},
                    },
                }},
                "entities": {"type": "array", "maxItems": 32, "items": {
                    "type": "object", "properties": {
                        "kind": {"type": "string"},
                        "value": {"type": ["string", "integer", "object"]},
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
            "required": ["goal", "effort", "output", "periods", "entities", "steps"],
            "additionalProperties": False,
        },
    )


PLANNING_INSTRUCTIONS = """For each request, either reply directly from the supplied context or call submit_request_plan once. A direct reply needs no lookup.

A plan has one goal, low or high answer effort, an available output form, explicit periods, entities and short steps. Use only capabilities needed for the request. Each step has id, capability, arguments, reason and depends_on. Independent steps have empty depends_on lists. A dependent argument can refer to an earlier result with {"step":"earlier_id","path":["field"]}.

For a utc_range, write kind, start and exclusive end in UTC. For a key, write kind, field and value using a registered time field. For a resolved period, write kind, step, selector and the exact result path advertised by the owning capability. Entities have kind and value; a value may use the same earlier-result reference as an argument. Declare resolved entities before later reads. Use the catalogue argument types, required fields, choices and bounds. Use a period key only when its owning capability defines it. To select a latest or current period, plan an earlier lookup that returns the key, declare a resolved period, then refer to that result. Empty periods mean current state; latest-N selectors are allowed only then. Name the sources the requester named. Offer other sources in the answer instead of reading them. Never broaden a period or source to make a lookup work.

Use low effort unless the answer needs substantial synthesis. After checked results arrive, answer from those results. Request more steps only for a remaining gap, and stay inside the declared scope unless a revision is needed. Mention a limit only if it changes the conclusion."""


ACTION_PLANNING_INSTRUCTIONS = """

Each capability is marked read, output, change or irreversible. Plan the reads needed to identify exact targets before choosing a change. If choosing targets needs judgment from read results, plan those reads first and add changes in the next round. Use an earlier result reference when a value is copied unchanged. Change steps share one preview in execution order; an irreversible step needs its own confirmation. The member confirms the preview before changes run in the background. Never say a change finished until the reported outcome confirms it.

When a listed command matches the request, include its command capability as a step. Use its registered option types and choices. Omit a required value when the member has not supplied or resolved it; the command will ask for all missing values. Never guess an ambiguous value. Code delivers each result at its required visibility."""
