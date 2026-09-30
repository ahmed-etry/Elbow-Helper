"""Connect checked command plans to feature adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from elbow_helper.domain.player_tags import normalize_player_tag

from ..models import AgentCapabilityEffect, RegisteredAgentTool
from .outcomes import CommandOutcome, command_reply
from .confirmation import ChangePreview, PreparedCommand
from .registry import (
    CommandAdapter, CommandCapability, PreparedCommandChange,
    build_command_capabilities,
)


def _missing_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return isinstance(value, (list, tuple, dict, set)) and not value


def _record_outcome(context: Any, outcome: CommandOutcome,
                    command_name: str) -> CommandOutcome:
    outcome = replace(outcome, command_name=command_name)
    if outcome.visibility == "private" and outcome.text:
        outcome = replace(outcome, text="",
                          private_parts=(outcome.text, *outcome.private_parts))
    if (outcome.visibility == "private" and not outcome.private_parts
            and not outcome.attachments and outcome.private_panel is None):
        outcome = replace(outcome, private_parts=(command_reply([outcome]),))
    context.state.command_outcomes.append(outcome)
    if outcome.visibility == "public":
        context.state.attachments.extend(outcome.attachments)
    return outcome


def build_command_tools(
    bot: Any, adapters: Sequence[CommandAdapter],
) -> tuple[dict[str, RegisteredAgentTool], dict[str, CommandCapability]]:
    capabilities = build_command_capabilities(bot, adapters)
    tools: dict[str, RegisteredAgentTool] = {}
    for name, capability in capabilities.items():
        async def handle(context, values, selected=capability):
            missing = tuple(
                option.description or option.name.replace("_", " ")
                for option in selected.option_info if option.name in selected.required
                and (option.name not in values or _missing_value(values[option.name]))
            )
            if not missing and selected.adapter.delivery == "confirm":
                if selected.adapter.prepare is None:
                    raise ValueError("Confirmed command needs a preview function")
                preview = await selected.adapter.prepare(context, values)
                if isinstance(preview, CommandOutcome):
                    outcome = _record_outcome(context, preview, selected.adapter.path)
                    return {"command": selected.adapter.path, "status": outcome.status,
                            "visibility": outcome.visibility}
                prepared_run = lambda: selected.adapter.run(context, values)
                if isinstance(preview, PreparedCommandChange):
                    prepared_run = preview.run
                    preview = preview.preview
                if not isinstance(preview, ChangePreview):
                    raise TypeError("Command preview is invalid")
                prepared = PreparedCommand(
                    selected.adapter.path, dict(values), preview,
                    prepared_run,
                    action_class=selected.adapter.classification,
                )
                context.state.command_proposals.append(prepared)
                return {"command": selected.adapter.path, "status": "confirmation_required"}
            outcome = (CommandOutcome.needs_input(missing) if missing
                       else await selected.adapter.run(context, values))
            if not isinstance(outcome, CommandOutcome):
                raise TypeError("Command adapter returned an invalid result")
            outcome = _record_outcome(context, outcome, selected.adapter.path)
            return {"command": selected.adapter.path, "status": outcome.status,
                    "visibility": outcome.visibility}
        tools[name] = RegisteredAgentTool(
            capability.definition, handle, AgentCapabilityEffect.COMMAND,
            capability.adapter.classification,
            capability.adapter.delivery == "confirm",
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
