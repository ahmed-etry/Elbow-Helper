"""Bounded, model-directed discovery of the fixed agent tool catalogue."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Mapping, Sequence

from elbow_helper.infrastructure.ai import AgentToolDefinition

from .models import RegisteredAgentTool


DISCOVERY_TOOL_NAME = "discover_agent_tools"
MAX_GROUPS_PER_CALL = 6
MAX_ACTIVE_CAPABILITY_TOOLS = 64
ALWAYS_AVAILABLE_GROUPS = frozenset({"knowledge_history"})
GROUP_SUMMARIES = {
    "discord_research": "Discord channels, threads, messages, and bounded research jobs.",
    "members_roles": "Discord members, linked accounts, role audits, and refresh jobs.",
    "achievements_events": "Member achievements, leaderboards, and event schedules.",
    "clan_operations": "Clan health, account movement, and clan reporting checks.",
    "wars": "Current and historical regular-war evidence.",
    "rosters": "Roster cycles, snapshots, reads, and comparisons.",
    "cwl": "CWL performance, ASS scopes, configured bonus calculations, and registered threads.",
    "transfers": "Pending clan-transfer requests.",
    "member_cases": "Lifecycle, hibernation, support, recruitment, examination, and leadership records.",
    "files": "Supported CSV, XLSX, and text attachment ingestion.",
    "knowledge_history": "Retained conversation history and approved community knowledge.",
    "planning_output": "Task instructions and request-shaped spreadsheets.",
}


def discovery_definition() -> AgentToolDefinition:
    catalogue = "; ".join(
        f"{name}: {summary}" for name, summary in GROUP_SUMMARIES.items()
    )
    return AgentToolDefinition(
        name=DISCOVERY_TOOL_NAME,
        description=(
            "Select the fixed capability groups needed for the next steps before using "
            "their tools. This replaces earlier optional groups, so include every group "
            "that must remain available. Choose by capability, not wording; combine "
            "groups for cross-feature work. "
            f"Available groups: {catalogue}"
        ),
        parameters={
            "type": "object",
            "properties": {
                "groups": {
                    "type": "array", "minItems": 1,
                    "maxItems": MAX_GROUPS_PER_CALL, "uniqueItems": True,
                    "items": {"type": "string", "enum": list(GROUP_SUMMARIES)},
                },
            },
            "required": ["groups"], "additionalProperties": False,
        },
    )


@dataclass(slots=True)
class ToolSelection:
    groups: Mapping[str, tuple[RegisteredAgentTool, ...]]
    active_names: set[str]

    @classmethod
    def for_registry(
        cls, registry: Mapping[str, RegisteredAgentTool],
        groups: Mapping[str, tuple[RegisteredAgentTool, ...]],
    ) -> ToolSelection | None:
        if set(groups) != set(GROUP_SUMMARIES):
            raise ValueError("Agent capability groups and summaries differ")
        grouped_sequence = [
            tool.definition.name for tools in groups.values() for tool in tools
        ]
        grouped_names = set(grouped_sequence)
        if len(grouped_sequence) != len(grouped_names):
            raise ValueError("Agent tools must belong to exactly one capability group")
        if set(registry) != grouped_names:
            return None
        return cls(
            groups=groups,
            active_names={
                tool.definition.name
                for name in ALWAYS_AVAILABLE_GROUPS
                for tool in groups[name]
            },
        )

    def definitions(self) -> tuple[AgentToolDefinition, ...]:
        return (
            discovery_definition(),
            *(tool.definition for name, tools in self.groups.items()
              if any(tool.definition.name in self.active_names for tool in tools)
              for tool in tools),
        )

    def activate(self, requested: Sequence[str]) -> dict[str, object]:
        requested_names = set(requested)
        baseline = {
            tool.definition.name
            for group_name in ALWAYS_AVAILABLE_GROUPS
            for tool in self.groups[group_name]
        }
        selected = {
            tool.definition.name
            for group_name in requested_names
            for tool in self.groups[group_name]
        }
        combined = baseline | selected
        if len(combined) > MAX_ACTIVE_CAPABILITY_TOOLS:
            return {
                "error": (
                    f"That selection would activate {len(combined)} tools; the bound is "
                    f"{MAX_ACTIVE_CAPABILITY_TOOLS}. Select fewer relevant groups."
                ),
                "active_groups": self.active_groups(),
            }
        self.active_names = combined
        return {
            "activated_groups": sorted(requested_names),
            "active_groups": self.active_groups(),
            "available_tools": sorted(combined),
            "instruction": (
                "Use the available tools. Select a new complete group set if later "
                "steps need different capabilities."
            ),
        }

    def active_groups(self) -> list[str]:
        return [
            name for name, tools in self.groups.items()
            if any(tool.definition.name in self.active_names for tool in tools)
        ]


def encoded_definitions(definitions: Sequence[AgentToolDefinition]) -> str:
    return json.dumps([{
        "type": "function",
        "function": {
            "name": tool.name, "description": tool.description,
            "parameters": tool.parameters,
        },
    } for tool in definitions], ensure_ascii=False, sort_keys=True)
