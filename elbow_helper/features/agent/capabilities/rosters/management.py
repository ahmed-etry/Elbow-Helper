"""Confirmed actions for standalone roster management controls."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any
import discord

from elbow_helper.infrastructure.ai import AgentToolDefinition
from elbow_helper.features.rosters.config import (
    ROSTER_PLAYER_COLUMN_MIN_WIDTH, ROSTER_PLAYER_COLUMN_MAX_WIDTH,
    ROSTER_DISCORD_COLUMN_MIN_WIDTH, ROSTER_DISCORD_COLUMN_MAX_WIDTH,
)

from ...access import require_evidence_access
from ...actions.contracts import ActionClass, ChangePreview, PreparedAction
from ...actions.outcomes import CommandOutcome
from ...models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ...wording import (
    ACTION_ROSTER_CONTROL_LINE,
    ACTION_ROSTER_CONTROL_STATUS,
    ACTION_ROSTER_POST_REFRESH,
    ACTION_ROSTER_CONTROL_MEMBER,
    ACTION_SIGNUP_ROLE,
    ACTION_ROSTER_CONTROL_COUNT,
    ACTION_ROSTER_CONTROL_OPEN,
    ACTION_ROSTER_CONTROL_CLOSE,
    ACTION_ROSTER_CONTROL_SHOW,
    ACTION_ROSTER_CONTROL_HIDE,
    ACTION_ROSTER_CONTROL_CLEAR,
    ACTION_FIELD_CHANGE,
    ACTION_ROSTER_LAYOUT_LINE,
    ACTION_ROSTER_LAYOUT_LABEL,
    ACTION_ROSTER_REFRESH_LINE,
    ACTION_ROSTER_REFRESH_MEMBER,
    ACTION_ROSTER_REFRESH_LABEL,
)
from ...discord_actions.safety import (
    check_member, check_post_access, check_role, resolve_channel, resolve_member,
)


async def _check_posts(context: AgentRequestContext,
                       posts: tuple[tuple[int, int], ...]) -> None:
    for channel_id, _ in posts:
        channel = await resolve_channel(context, channel_id)
        check_post_access(channel, context.member, context.guild.me)


async def _check_members(context: AgentRequestContext,
                         roster: Any, member_ids: tuple[int, ...]) -> None:
    role = context.guild.get_role(roster.role_id) if roster.role_id else None
    if role is not None:
        check_role(role, context.guild, context.guild.me, {})
    for member_id in member_ids:
        member = await resolve_member(context.guild, member_id)
        check_member(member, context.guild.me)


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
    async def prepare_state(context: AgentRequestContext,
                            values: Mapping[str, Any]) -> Mapping[str, Any]:
        selected = next((spec for spec in _OPERATIONS[:4]
                         if spec[0] == {
                             "open": "open_roster", "close": "close_roster",
                             "show": "show_roster_controls",
                             "hide": "hide_roster_controls",
                         }.get(values["operation"])), None)
        if selected is None:
            raise ValueError("Choose open, close, show or hide.")
        name, operation, classification, label = selected
        return await _prepare(context, values, name, operation, classification, label)

    tools = [RegisteredAgentTool(AgentToolDefinition(
        name="set_roster_state",
        description="Open or close a roster, or show or hide its signup controls after confirmation.",
        parameters={"type": "object", "properties": {
            "roster_id": {"type": "integer", "minimum": 1},
            "operation": {"type": "string", "enum": ["open", "close", "show", "hide"]},
        }, "required": ["roster_id", "operation"], "additionalProperties": False},
    ), prepare_state, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE, True)]
    name, operation, classification, label = _OPERATIONS[4]
    async def prepare_clear(context: AgentRequestContext,
                            values: Mapping[str, Any]) -> Mapping[str, Any]:
        return await _prepare(context, values, name, operation, classification, label)
    tools.append(RegisteredAgentTool(
        AgentToolDefinition(name=name, description="Clear a roster's signups after confirmation.",
                            parameters=schema),
        prepare_clear, AgentCapabilityEffect.COMMAND, classification, True,
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
        raise ValueError('That roster is unavailable.')
    roster_id = values["roster_id"]
    state = await workflow.roster_management_state(roster_id)
    if state is None or state["roster"].guild_id != context.guild.id:
        raise ValueError('That roster is unavailable.')
    roster = state["roster"]
    await _check_posts(context, state["posts"])
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
    lines.extend(ACTION_ROSTER_POST_REFRESH.format(message_id=message_id,
                                                   channel=f"<#{channel_id}>")
                 for channel_id, message_id in state["posts"])

    async def recheck() -> bool:
        current = await workflow.roster_management_state(roster_id)
        if current is None or current != state:
            return False
        try:
            await _check_posts(context, state["posts"])
            if role is not None:
                check_role(role, context.guild, context.guild.me, {})
            for member in members:
                check_member(member, context.guild.me)
        except (discord.DiscordException, ValueError, RuntimeError, KeyError, TypeError, OSError):
            return False
        return True

    async def run() -> CommandOutcome:
        status = await workflow.refresh_roster(roster_id)
        if status != "complete":
            raise ValueError("That roster couldn't be refreshed.")
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
        raise ValueError('That roster is unavailable.')
    roster_id = values["roster_id"]
    state = await workflow.roster_layout_state(roster_id)
    if state is None or state[0].guild_id != context.guild.id:
        raise ValueError('That roster is unavailable.')
    roster, layout = state
    before = asdict(layout)
    changes = {field: values[field] for field in before if field in values
               and values[field] != before[field]}
    if not changes:
        return {"status": "no_change"}
    posts = (await workflow.roster_edit_state(roster))["posts"]
    await _check_posts(context, posts)
    lines = [ACTION_ROSTER_LAYOUT_LINE.format(name=roster.name)]
    lines.extend(ACTION_FIELD_CHANGE.format(
        field=field.replace("_", " ").title(), old=before[field], new=value)
        for field, value in changes.items())
    lines.extend(ACTION_ROSTER_POST_REFRESH.format(
        message_id=message_id, channel=f"<#{channel_id}>")
        for channel_id, message_id in posts)

    async def recheck() -> bool:
        live = await workflow.roster_layout_state(roster_id)
        if live is None or live[0] != roster or live[1] != layout:
            return False
        try:
            await _check_posts(context, posts)
        except (discord.DiscordException, ValueError, RuntimeError, KeyError, TypeError, OSError):
            return False
        return True

    async def run() -> CommandOutcome:
        updated, result = await workflow.set_roster_layout(roster_id, **changes)
        if updated is None:
            raise ValueError('That roster is unavailable.')
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
        raise ValueError('That roster is unavailable.')
    roster_id = log["targets"]["roster_id"]
    state = await workflow.roster_layout_state(roster_id)
    if state is None or state[0].guild_id != context.guild.id:
        raise ValueError('That roster is unavailable.')
    roster, layout = state
    prior = log["before"]["layout"]
    expected = log["after"]["layout"]
    posts = (await workflow.roster_edit_state(roster))["posts"]
    await _check_posts(context, posts)
    lines = [ACTION_ROSTER_LAYOUT_LINE.format(name=roster.name)]
    lines.extend(ACTION_FIELD_CHANGE.format(
        field=field.replace("_", " ").title(), old=getattr(layout, field), new=value)
        for field, value in prior.items() if getattr(layout, field) != value)

    async def recheck() -> bool:
        live = await workflow.roster_layout_state(roster_id)
        if live is None or asdict(live[1]) != expected:
            return False
        try:
            await _check_posts(context, posts)
        except (discord.DiscordException, ValueError, RuntimeError, KeyError, TypeError, OSError):
            return False
        return True

    async def run() -> CommandOutcome:
        updated, result = await workflow.set_roster_layout(roster_id, **prior)
        if updated is None:
            raise ValueError('That roster is unavailable.')
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
        raise ValueError('That roster is unavailable.')
    roster_id = values["roster_id"]
    state = await workflow.roster_management_state(roster_id)
    if state is None or state["roster"].guild_id != context.guild.id:
        raise ValueError('That roster is unavailable.')
    roster = state["roster"]
    await _check_posts(context, state["posts"])
    check_members = operation == "clear" or (operation == "open" and roster.reset_on_open)
    if check_members:
        await _check_members(context, roster, state["member_ids"])
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
            lines.append(ACTION_SIGNUP_ROLE.format(role=role.mention))
        lines.extend(ACTION_ROSTER_CONTROL_MEMBER.format(member=f"<@{member_id}>")
                     for member_id in state["member_ids"])
    lines.extend(ACTION_ROSTER_POST_REFRESH.format(message_id=message_id,
                                                   channel=f"<#{channel_id}>")
                 for channel_id, message_id in state["posts"])

    def signature(snapshot: Mapping[str, Any]) -> tuple[Any, ...]:
        item = snapshot["roster"]
        return (item.guild_id, item.status, item.buttons_hidden, item.active_cycle_id,
                item.role_id, snapshot["account_count"], snapshot["member_ids"], snapshot["posts"])

    async def recheck() -> bool:
        current = await workflow.roster_management_state(roster_id)
        if current is None or signature(current) != signature(state):
            return False
        try:
            await _check_posts(context, current["posts"])
            if check_members:
                await _check_members(context, current["roster"], current["member_ids"])
        except (discord.DiscordException, ValueError, RuntimeError, KeyError, TypeError, OSError):
            return False
        return True

    async def run() -> CommandOutcome:
        if operation == "clear":
            result = await workflow.clear_roster_signups(roster_id)
            text = result.message
        else:
            text = await workflow.change_roster_management(roster_id, operation)
        return CommandOutcome("complete", "private", text=text)

    context.state.command_proposals.append(PreparedAction(
        "clear_roster_signups" if operation == "clear" else "set_roster_state",
        {"roster_id": roster_id, **({"operation": values["operation"]}
                                  if operation != "clear" else {})},
        ChangePreview(tuple(lines), recheck, summary=label),
        run, action_class=classification,
    ))
    return {"status": "confirmation_required"}
