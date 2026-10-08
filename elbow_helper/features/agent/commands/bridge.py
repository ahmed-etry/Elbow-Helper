"""Connect checked command plans to feature adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from ..models import AgentCapabilityEffect, RegisteredAgentTool
from ..actions.outcomes import ActionOutcome, command_reply
from ..actions.contracts import ChangePreview, PreparedAction
from ..wording import ACTION_UNAVAILABLE
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
    from ..engine.result_hints import RETURN_HINTS
    capabilities = build_command_capabilities(bot, adapters)
    tools: dict[str, RegisteredAgentTool] = {}
    for name, capability in capabilities.items():
        async def handle(context, values, selected=capability):
            if selected.visible_to is not None:
                member = context.guild.get_member(context.member.id)
                if member is None or not any(role.id in selected.visible_to for role in member.roles):
                    return {"error": ACTION_UNAVAILABLE}
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
                    _authorized_run(context, selected, prepared_run),
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
            capability.adapter.classification, returns=RETURN_HINTS.get(name),
        )
    return tools, capabilities


def _authorized_run(context, selected, run):
    async def checked():
        if selected.visible_to is not None:
            member = context.guild.get_member(context.member.id)
            if member is None or not any(role.id in selected.visible_to for role in member.roles):
                return ActionOutcome.unavailable()
        return await run()
    return checked
