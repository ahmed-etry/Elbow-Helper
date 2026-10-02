"""Connect checked command plans to feature adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from elbow_helper.domain.player_tags import normalize_player_tag

from ..models import AgentCapabilityEffect, RegisteredAgentTool
from ..actions.outcomes import ActionOutcome, command_reply
from ..actions.contracts import ChangePreview, PreparedAction
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


def _option_data(option: Any) -> dict[str, Any]:
    return {
        "name": option.name,
        "description": option.description,
        "choices": [
            {"label": label, "value": value}
            for label, value in zip(option.choices, option.choice_values)
        ],
    }


def _with_option_data(outcome: ActionOutcome, selected: CommandCapability,
                      values: Mapping[str, Any]) -> ActionOutcome:
    if outcome.status != "needs_input" or outcome.missing_options:
        return outcome
    return replace(outcome, missing_options=tuple(
        _option_data(option) for option in selected.option_info
        if option.name in selected.required or option.name not in values
    ))


def _record_outcome(context: Any, outcome: ActionOutcome,
                    command_name: str) -> ActionOutcome:
    outcome = replace(outcome, command_name=command_name)
    if outcome.visibility == "private" and outcome.text:
        outcome = replace(outcome, text="",
                          private_parts=(outcome.text, *outcome.private_parts))
    if (outcome.visibility == "private" and not outcome.private_parts
            and not outcome.attachments and outcome.private_panel is None):
        outcome = replace(outcome, private_parts=(command_reply([outcome]),))
    context.state.outcomes.append(outcome)
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
            missing_options = tuple(
                option for option in selected.option_info
                if option.name in selected.required
                and (option.name not in values or _missing_value(values[option.name]))
            )
            missing = tuple(option.description or option.name.replace("_", " ")
                            for option in missing_options)
            if not missing and selected.adapter.delivery == "confirm":
                if selected.adapter.prepare is None:
                    raise ValueError("Confirmed command needs a preview function")
                preview = await selected.adapter.prepare(context, values)
                if isinstance(preview, ActionOutcome):
                    outcome = _record_outcome(
                        context, _with_option_data(preview, selected, values), selected.adapter.path,
                    )
                    return {"command": selected.adapter.path, "status": outcome.status,
                            "visibility": outcome.visibility}
                prepared_run = lambda: selected.adapter.run(context, values)
                if isinstance(preview, PreparedCommandChange):
                    prepared_run = preview.run
                    preview = preview.preview
                if not isinstance(preview, ChangePreview):
                    raise TypeError("Command preview is invalid")
                prepared = PreparedAction(
                    selected.adapter.path, dict(values), preview,
                    prepared_run,
                    action_class=selected.adapter.classification,
                )
                context.state.proposed_changes.append(prepared)
                return {"command": selected.adapter.path, "status": "confirmation_required"}
            outcome = (ActionOutcome.needs_input(
                missing, options=tuple(_option_data(option) for option in missing_options),
            ) if missing
                       else await selected.adapter.run(context, values))
            if not isinstance(outcome, ActionOutcome):
                raise TypeError("Command adapter returned an invalid result")
            outcome = _with_option_data(outcome, selected, values)
            outcome = _record_outcome(context, outcome, selected.adapter.path)
            return {"command": selected.adapter.path, "status": outcome.status,
                    "visibility": outcome.visibility}
        tools[name] = RegisteredAgentTool(
            capability.definition, handle, AgentCapabilityEffect.COMMAND,
            capability.adapter.classification,
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
