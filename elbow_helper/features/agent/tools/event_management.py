"""Confirmed changes exposed by the event management panel."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..actions.contracts import ActionClass, ChangePreview, PreparedAction
from ..commands.outcomes import CommandOutcome
from ..models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ..wording import (
    ACTION_EVENT_MANAGE_CATEGORY, ACTION_EVENT_MANAGE_DELETE,
    ACTION_EVENT_MANAGE_ENABLED, ACTION_EVENT_MANAGE_MOVE,
    ACTION_EVENT_MANAGE_RESET, ACTION_EVENT_MANAGE_CHANNEL,
    ACTION_EVENT_MANAGE_LABEL, ACTION_EVENT_MANAGE_UNAVAILABLE,
)


def event_management_tools() -> tuple[RegisteredAgentTool, ...]:
    specs = (
        ("set_event_enabled", "Set whether an event tracker is enabled.", ActionClass.CHANGE,
         {"enabled": {"type": "boolean"}}),
        ("set_event_category", "Set an event tracker's category.", ActionClass.CHANGE,
         {"category_id": {"type": "integer", "minimum": 1}}),
        ("move_event", "Move an event tracker to a requested place in the list.", ActionClass.CHANGE,
         {"position": {"type": "integer", "minimum": 1},
          "edge": {"type": "string", "enum": ["top", "bottom"]}}),
        ("restore_event_defaults", "Restore a preset event tracker's default settings.", ActionClass.IRREVERSIBLE, {}),
        ("delete_event", "Delete a custom event tracker and its voice channel.", ActionClass.IRREVERSIBLE, {}),
    )
    tools = []
    for name, description, classification, extra in specs:
        async def prepare(context: AgentRequestContext, values: Mapping[str, Any],
                          operation=name, action_class=classification) -> Mapping[str, Any]:
            return await _prepare(context, values, operation, action_class)
        tools.append(RegisteredAgentTool(AgentToolDefinition(
            name=name, description=description,
            parameters={"type": "object", "properties": {
                "event": {"type": "string", "minLength": 1}, **extra,
            }, "required": ["event", *(list(extra) if name == "set_event_enabled" else [])],
                "additionalProperties": False},
        ), prepare, AgentCapabilityEffect.COMMAND, classification, True))
    return tuple(tools)


async def _prepare(context: AgentRequestContext, values: Mapping[str, Any],
                   operation: str, classification: ActionClass) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("EventStatsCog") or context.bot.get_cog("EventStats")
    if workflow is None:
        raise ValueError(ACTION_EVENT_MANAGE_UNAVAILABLE)
    state = workflow.event_management_state(values["event"])
    if state is None:
        raise ValueError(ACTION_EVENT_MANAGE_UNAVAILABLE)
    event = state["event"]
    key = event["key"]
    lines: list[str] = []
    if operation == "set_event_enabled":
        enabled = values["enabled"]
        if event["enabled"] == enabled:
            return {"status": "no_change"}
        lines.append(ACTION_EVENT_MANAGE_ENABLED.format(
            name=event["name"], old=event["enabled"], new=enabled))
    elif operation == "set_event_category":
        category_id = values.get("category_id")
        if category_id is not None:
            category = context.guild.get_channel(category_id)
            if not isinstance(category, discord.CategoryChannel):
                raise ValueError(ACTION_EVENT_MANAGE_UNAVAILABLE)
        else:
            category = None
        if event.get("category_id") == category_id:
            return {"status": "no_change"}
        old = context.guild.get_channel(event.get("category_id")) if event.get("category_id") else None
        lines.append(ACTION_EVENT_MANAGE_CATEGORY.format(
            name=event["name"], old=old.name if old else "None",
            new=category.name if category else "None"))
    elif operation == "move_event":
        edge = values.get("edge")
        position = values.get("position")
        if (edge is None) == (position is None):
            raise ValueError(ACTION_EVENT_MANAGE_UNAVAILABLE)
        target = (0 if edge == "top" else state["count"] - 1) if edge else position - 1
        if not 0 <= target < state["count"]:
            raise ValueError(ACTION_EVENT_MANAGE_UNAVAILABLE)
        if state["position"] == target:
            return {"status": "no_change"}
        lines.append(ACTION_EVENT_MANAGE_MOVE.format(
            name=event["name"], old=state["position"] + 1, new=target + 1))
    elif operation == "restore_event_defaults":
        if event["source"] != "preset":
            raise ValueError(ACTION_EVENT_MANAGE_UNAVAILABLE)
        lines.append(ACTION_EVENT_MANAGE_RESET.format(name=event["name"]))
    elif operation == "delete_event":
        if event["source"] != "custom":
            raise ValueError(ACTION_EVENT_MANAGE_UNAVAILABLE)
        lines.append(ACTION_EVENT_MANAGE_DELETE.format(name=event["name"]))
        if event.get("channel_id"):
            lines.append(ACTION_EVENT_MANAGE_CHANNEL.format(channel=f"<#{event['channel_id']}>"))
    else:
        raise ValueError(ACTION_EVENT_MANAGE_UNAVAILABLE)

    async def recheck() -> bool:
        live = workflow.event_management_state(key)
        return live is not None and live == state

    async def run() -> CommandOutcome:
        if operation == "set_event_enabled":
            ok = workflow.toggle_event(key)
        elif operation == "set_event_category":
            ok = workflow.set_event_category(key, category_id)
        elif operation == "move_event":
            ok = workflow.move_event_to_position(key, target)
        elif operation == "restore_event_defaults":
            ok = workflow.reset_preset_event(key)
        else:
            ok, message = await workflow.delete_custom_event(context.guild, key)
        if not ok:
            raise ValueError(ACTION_EVENT_MANAGE_UNAVAILABLE)
        await workflow.force_refresh(context.guild)
        return CommandOutcome("complete", "private", text=(
            message if operation == "delete_event" else lines[0]))

    context.state.command_proposals.append(PreparedAction(
        operation, {"event": key},
        ChangePreview(tuple(lines), recheck, summary=ACTION_EVENT_MANAGE_LABEL),
        run, action_class=classification,
    ))
    return {"status": "confirmation_required"}
