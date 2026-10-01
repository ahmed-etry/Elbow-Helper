"""Confirmed actions for standalone roster management controls."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..actions.contracts import ActionClass, ChangePreview, PreparedAction
from ..commands.outcomes import CommandOutcome
from ..models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ..wording import (
    ACTION_ROSTER_UNAVAILABLE, ACTION_ROSTER_CONTROL_LINE,
    ACTION_ROSTER_CONTROL_STATUS, ACTION_ROSTER_CONTROL_POST,
    ACTION_ROSTER_CONTROL_MEMBER, ACTION_ROSTER_CONTROL_ROLE,
    ACTION_ROSTER_CONTROL_COUNT, ACTION_ROSTER_CONTROL_OPEN,
    ACTION_ROSTER_CONTROL_CLOSE, ACTION_ROSTER_CONTROL_SHOW,
    ACTION_ROSTER_CONTROL_HIDE, ACTION_ROSTER_CONTROL_CLEAR,
)


_OPERATIONS = (
    ("open_roster", "open", ActionClass.CHANGE, ACTION_ROSTER_CONTROL_OPEN),
    ("close_roster", "close", ActionClass.CHANGE, ACTION_ROSTER_CONTROL_CLOSE),
    ("show_roster_controls", "toggle_buttons", ActionClass.CHANGE, ACTION_ROSTER_CONTROL_SHOW),
    ("hide_roster_controls", "toggle_buttons", ActionClass.CHANGE, ACTION_ROSTER_CONTROL_HIDE),
    ("clear_roster_signups", "clear", ActionClass.IRREVERSIBLE, ACTION_ROSTER_CONTROL_CLEAR),
)


def roster_management_tools() -> tuple[RegisteredAgentTool, ...]:
    schema = {"type": "object", "properties": {
        "roster_id": {"type": "integer", "minimum": 1},
    }, "required": ["roster_id"], "additionalProperties": False}
    tools = []
    for name, operation, classification, label in _OPERATIONS:
        async def prepare(context: AgentRequestContext, values: Mapping[str, Any],
                          selected=name, action=operation, action_class=classification,
                          summary=label) -> Mapping[str, Any]:
            return await _prepare(context, values, selected, action, action_class, summary)
        tools.append(RegisteredAgentTool(
            AgentToolDefinition(name=name, description=f"{label} for an existing roster after confirmation.",
                                parameters=schema),
            prepare, AgentCapabilityEffect.COMMAND, classification, True,
        ))
    return tuple(tools)


async def _prepare(context: AgentRequestContext, values: Mapping[str, Any],
                   name: str, operation: str, classification: ActionClass,
                   label: str) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ValueError(ACTION_ROSTER_UNAVAILABLE)
    roster_id = values["roster_id"]
    state = await workflow.roster_management_state(roster_id)
    if state is None or state["roster"].guild_id != context.guild.id:
        raise ValueError(ACTION_ROSTER_UNAVAILABLE)
    roster = state["roster"]
    if name == "show_roster_controls" and not roster.buttons_hidden:
        return {"status": "no_change"}
    if name == "hide_roster_controls" and roster.buttons_hidden:
        return {"status": "no_change"}
    lines = [
        ACTION_ROSTER_CONTROL_LINE.format(action=label, name=roster.name),
        ACTION_ROSTER_CONTROL_STATUS.format(status=roster.status),
    ]
    if operation == "clear":
        lines.append(ACTION_ROSTER_CONTROL_COUNT.format(count=state["account_count"]))
        role = context.guild.get_role(roster.role_id) if roster.role_id else None
        if role is not None and state["member_ids"]:
            lines.append(ACTION_ROSTER_CONTROL_ROLE.format(role=role.mention))
        lines.extend(ACTION_ROSTER_CONTROL_MEMBER.format(member=f"<@{member_id}>")
                     for member_id in state["member_ids"])
    lines.extend(ACTION_ROSTER_CONTROL_POST.format(message_id=message_id,
                                                   channel=f"<#{channel_id}>")
                 for channel_id, message_id in state["posts"])

    def signature(snapshot: Mapping[str, Any]) -> tuple[Any, ...]:
        item = snapshot["roster"]
        return (item.guild_id, item.status, item.buttons_hidden, item.active_cycle_id,
                item.role_id, snapshot["account_count"], snapshot["member_ids"], snapshot["posts"])

    async def recheck() -> bool:
        current = await workflow.roster_management_state(roster_id)
        return current is not None and signature(current) == signature(state)

    async def run() -> CommandOutcome:
        if operation == "clear":
            result = await workflow.clear_roster_signups(roster_id)
            text = result.message
        else:
            text = await workflow.change_roster_management(roster_id, operation)
        return CommandOutcome("complete", "private", text=text)

    context.state.command_proposals.append(PreparedAction(
        name, {"roster_id": roster_id},
        ChangePreview(tuple(lines), recheck, summary=label),
        run, action_class=classification,
    ))
    return {"status": "confirmation_required"}
