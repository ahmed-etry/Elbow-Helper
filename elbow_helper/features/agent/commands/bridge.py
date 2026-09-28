"""Connect checked command plans to feature adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from elbow_helper.domain.player_tags import normalize_player_tag

from ..models import AgentCapabilityEffect, RegisteredAgentTool
from .outcomes import CommandOutcome
from .registry import CommandAdapter, CommandCapability, build_command_capabilities


def build_command_tools(
    bot: Any, adapters: Sequence[CommandAdapter],
) -> tuple[dict[str, RegisteredAgentTool], dict[str, CommandCapability]]:
    capabilities = build_command_capabilities(bot, adapters)
    tools: dict[str, RegisteredAgentTool] = {}
    for name, capability in capabilities.items():
        async def handle(context, values, selected=capability):
            missing = next((field for field in selected.required if not values.get(field)), "")
            outcome = (CommandOutcome.needs_input(missing) if missing
                       else await selected.adapter.run(context, values))
            if not isinstance(outcome, CommandOutcome):
                raise TypeError("Command adapter returned an invalid result")
            context.state.command_outcomes.append(outcome)
            return {"command": selected.adapter.path, "status": outcome.status,
                    "visibility": outcome.visibility}
        tools[name] = RegisteredAgentTool(
            capability.definition, handle, AgentCapabilityEffect.COMMAND,
        )
    return tools, capabilities


def check_command_plan(
    plan: Mapping[str, Any], capabilities: Mapping[str, CommandCapability],
    named_sources: Mapping[str, frozenset[Any]],
) -> str:
    for step in plan["steps"]:
        capability = capabilities.get(step["capability"])
        if capability is None:
            continue
        values = step["arguments"]
        for option, kind in capability.adapter.entity_options:
            value = values.get(option)
            named = named_sources.get(kind, frozenset())
            if value is None or not named or isinstance(value, dict):
                continue
            selected = normalize_player_tag(value) if kind == "clash_account" else str(value)
            if selected is None or str(selected) not in {str(item) for item in named}:
                return "Offer other named sources instead of running this command for them."
        if capability.adapter.check_period is not None:
            issue = capability.adapter.check_period(plan, values)
            if issue:
                return issue
    return ""
