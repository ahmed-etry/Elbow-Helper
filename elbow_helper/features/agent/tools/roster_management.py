"""Confirmed actions for standalone roster management controls."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from elbow_helper.infrastructure.ai import AgentToolDefinition
from elbow_helper.features.rosters.config import (
    ROSTER_PLAYER_COLUMN_MIN_WIDTH, ROSTER_PLAYER_COLUMN_MAX_WIDTH,
    ROSTER_DISCORD_COLUMN_MIN_WIDTH, ROSTER_DISCORD_COLUMN_MAX_WIDTH,
)

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
    ACTION_ROSTER_LAYOUT_FIELD, ACTION_ROSTER_LAYOUT_LINE,
    ACTION_ROSTER_LAYOUT_LABEL, ACTION_ROSTER_LAYOUT_POST,
    ACTION_ROSTER_REFRESH_LINE, ACTION_ROSTER_REFRESH_MEMBER,
    ACTION_ROSTER_REFRESH_LABEL, ACTION_ROSTER_REFRESH_UNAVAILABLE,
)
from .discord_safety import check_member, check_role, resolve_member


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
    tools.append(RegisteredAgentTool(AgentToolDefinition(
        name="refresh_roster",
        description="Refresh a roster's account details, signup roles and post after confirmation.",
        parameters=schema,
    ), prepare_roster_refresh, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE, True))
    tools.append(RegisteredAgentTool(AgentToolDefinition(
        name="set_roster_layout",
        description="Set roster columns or displayed name lengths after confirmation.",
        parameters={"type": "object", "properties": {
            "roster_id": {"type": "integer", "minimum": 1},
            "show_townhall": {"type": "boolean"},
            "show_discord": {"type": "boolean"},
            "show_clan": {"type": "boolean"},
            "player_width": {"type": "integer", "minimum": ROSTER_PLAYER_COLUMN_MIN_WIDTH,
                             "maximum": ROSTER_PLAYER_COLUMN_MAX_WIDTH},
            "discord_width": {"type": "integer", "minimum": ROSTER_DISCORD_COLUMN_MIN_WIDTH,
                              "maximum": ROSTER_DISCORD_COLUMN_MAX_WIDTH},
        }, "required": ["roster_id"], "additionalProperties": False},
    ), prepare_roster_layout, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE, True))
    return tuple(tools)


async def prepare_roster_refresh(context: AgentRequestContext,
                                 values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ValueError(ACTION_ROSTER_UNAVAILABLE)
    roster_id = values["roster_id"]
    state = await workflow.roster_management_state(roster_id)
    if state is None or state["roster"].guild_id != context.guild.id:
        raise ValueError(ACTION_ROSTER_UNAVAILABLE)
    roster = state["roster"]
    role = context.guild.get_role(roster.role_id) if roster.role_id else None
    if role is not None:
        check_role(role, context.guild, context.guild.me, {})
    members = [await resolve_member(context.guild, member_id)
               for member_id in state["member_ids"]]
    for member in members:
        check_member(member, context.guild.me)
    lines = [ACTION_ROSTER_REFRESH_LINE.format(name=roster.name)]
    lines.extend(ACTION_ROSTER_REFRESH_MEMBER.format(member=member.mention)
                 for member in members)
    lines.extend(ACTION_ROSTER_CONTROL_POST.format(message_id=message_id,
                                                   channel=f"<#{channel_id}>")
                 for channel_id, message_id in state["posts"])

    async def recheck() -> bool:
        current = await workflow.roster_management_state(roster_id)
        if current is None or current != state:
            return False
        try:
            if role is not None:
                check_role(role, context.guild, context.guild.me, {})
            for member in members:
                check_member(member, context.guild.me)
        except ValueError:
            return False
        return True

    async def run() -> CommandOutcome:
        status = await workflow.refresh_roster(roster_id)
        if status != "complete":
            raise ValueError(ACTION_ROSTER_REFRESH_UNAVAILABLE)
        return CommandOutcome("complete", "private", text=ACTION_ROSTER_REFRESH_LABEL)

    context.state.command_proposals.append(PreparedAction(
        "refresh_roster", dict(values),
        ChangePreview(tuple(lines), recheck, summary=ACTION_ROSTER_REFRESH_LABEL),
        run, action_class=ActionClass.CHANGE,
    ))
    return {"status": "confirmation_required"}


async def prepare_roster_layout(context: AgentRequestContext,
                                values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ValueError(ACTION_ROSTER_UNAVAILABLE)
    roster_id = values["roster_id"]
    state = await workflow.roster_layout_state(roster_id)
    if state is None or state[0].guild_id != context.guild.id:
        raise ValueError(ACTION_ROSTER_UNAVAILABLE)
    roster, layout = state
    before = asdict(layout)
    changes = {field: values[field] for field in before if field in values
               and values[field] != before[field]}
    if not changes:
        return {"status": "no_change"}
    posts = (await workflow.roster_edit_state(roster))["posts"]
    lines = [ACTION_ROSTER_LAYOUT_LINE.format(name=roster.name)]
    lines.extend(ACTION_ROSTER_LAYOUT_FIELD.format(
        field=field.replace("_", " ").title(), old=before[field], new=value)
        for field, value in changes.items())
    lines.extend(ACTION_ROSTER_LAYOUT_POST.format(
        message_id=message_id, channel=f"<#{channel_id}>")
        for channel_id, message_id in posts)

    async def recheck() -> bool:
        live = await workflow.roster_layout_state(roster_id)
        return live is not None and live[0] == roster and live[1] == layout

    async def run() -> CommandOutcome:
        updated, result = await workflow.set_roster_layout(roster_id, **changes)
        if updated is None:
            raise ValueError(ACTION_ROSTER_UNAVAILABLE)
        return CommandOutcome("complete", "private",
                              text=ACTION_ROSTER_LAYOUT_LINE.format(name=updated.name),
                              after={"layout": asdict(result)})

    context.state.command_proposals.append(PreparedAction(
        "set_roster_layout", {"roster_id": roster_id},
        ChangePreview(tuple(lines), recheck, summary=ACTION_ROSTER_LAYOUT_LABEL,
                      before={"layout": before}), run,
    ))
    return {"status": "confirmation_required"}


async def prepare_roster_layout_undo(context: AgentRequestContext,
                                     log: Mapping[str, Any]) -> PreparedAction:
    workflow = context.bot.get_cog("Rosters")
    if workflow is None:
        raise ValueError(ACTION_ROSTER_UNAVAILABLE)
    roster_id = log["targets"]["roster_id"]
    state = await workflow.roster_layout_state(roster_id)
    if state is None or state[0].guild_id != context.guild.id:
        raise ValueError(ACTION_ROSTER_UNAVAILABLE)
    roster, layout = state
    prior = log["before"]["layout"]
    expected = log["after"]["layout"]
    lines = [ACTION_ROSTER_LAYOUT_LINE.format(name=roster.name)]
    lines.extend(ACTION_ROSTER_LAYOUT_FIELD.format(
        field=field.replace("_", " ").title(), old=getattr(layout, field), new=value)
        for field, value in prior.items() if getattr(layout, field) != value)

    async def recheck() -> bool:
        live = await workflow.roster_layout_state(roster_id)
        return live is not None and asdict(live[1]) == expected

    async def run() -> CommandOutcome:
        updated, result = await workflow.set_roster_layout(roster_id, **prior)
        if updated is None:
            raise ValueError(ACTION_ROSTER_UNAVAILABLE)
        return CommandOutcome("complete", "private",
                              text=ACTION_ROSTER_LAYOUT_LINE.format(name=updated.name),
                              after={"layout": asdict(result)})

    return PreparedAction(
        "undo_roster_layout", {"roster_id": roster_id},
        ChangePreview(tuple(lines), recheck, summary=ACTION_ROSTER_LAYOUT_LABEL,
                      before={"layout": expected}), run,
    )


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
