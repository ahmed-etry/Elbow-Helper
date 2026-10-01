"""Confirmed changes exposed by the event management panel."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..actions.contracts import ActionClass, ChangePreview, PreparedAction
from ..commands.outcomes import CommandOutcome, embed_text
from ..models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ..wording import (
    ACTION_EVENT_MANAGE_CATEGORY, ACTION_EVENT_MANAGE_DELETE,
    ACTION_EVENT_MANAGE_ENABLED, ACTION_EVENT_MANAGE_MOVE,
    ACTION_EVENT_MANAGE_RESET, ACTION_EVENT_MANAGE_CHANNEL,
    ACTION_EVENT_MANAGE_LABEL, ACTION_EVENT_MANAGE_UNAVAILABLE,
    ACTION_EVENT_FORM_CREATE, ACTION_EVENT_FORM_EDIT, ACTION_EVENT_FORM_FIELD,
    ACTION_EVENT_FORM_CHANNEL, ACTION_EVENT_FORM_LABEL,
    ACTION_EVENT_FORM_REFRESH,
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
    form_fields = {
        "name": {"type": "string", "minLength": 1},
        "start": {"type": "string"}, "end": {"type": "string"},
        "timezone": {"type": "string"},
        "grace_hours": {"type": "integer", "minimum": 0},
    }
    for name, operation, classification in (
        ("create_event_tracker", "create", ActionClass.IRREVERSIBLE),
        ("edit_event_tracker", "edit", ActionClass.CHANGE),
    ):
        async def prepare(context: AgentRequestContext, values: Mapping[str, Any],
                          selected=operation, action_class=classification) -> Mapping[str, Any]:
            return await _prepare_form(context, values, selected, action_class)
        tools.append(RegisteredAgentTool(AgentToolDefinition(
            name=name, description=f"{operation.title()} a one-time event tracker after confirmation.",
            parameters={"type": "object", "properties": {
                **({"event": {"type": "string"}} if operation == "edit" else {}),
                **form_fields,
            }, "required": [*(["event"] if operation == "edit" else []),
                             "name", "start", "end", "timezone"],
                "additionalProperties": False},
        ), prepare, AgentCapabilityEffect.COMMAND, classification, True))
    tools.append(RegisteredAgentTool(AgentToolDefinition(
        name="edit_preset_event",
        description="Change a preset event tracker name and grace period after confirmation.",
        parameters={"type": "object", "properties": {
            "event": {"type": "string"}, "name": {"type": "string", "minLength": 1},
            "grace_hours": {"type": "integer", "minimum": 0},
        }, "required": ["event", "name"], "additionalProperties": False},
    ), _prepare_preset, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE, True))
    return tuple(tools)


async def _prepare_form(context: AgentRequestContext, values: Mapping[str, Any],
                        operation: str, classification: ActionClass) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("EventStatsCog")
    if workflow is None:
        raise ValueError(ACTION_EVENT_MANAGE_UNAVAILABLE)
    current = workflow.event_management_state(values["event"]) if operation == "edit" else None
    if operation == "edit" and (current is None or current["event"]["source"] != "custom"):
        raise ValueError(ACTION_EVENT_MANAGE_UNAVAILABLE)
    prepared, issue = workflow.prepare_one_time_event_values(
        name=values["name"], start_text=values["start"],
        end_text=values["end"], timezone_text=values["timezone"],
        grace_text=str(values.get("grace_hours", "")),
    )
    if issue:
        raise ValueError(issue)
    lines = [ACTION_EVENT_FORM_CREATE.format(name=prepared["name"]) if current is None
             else ACTION_EVENT_FORM_EDIT.format(name=current["event"]["name"])]
    before = current["event"] if current else {}
    for field, value in prepared.items():
        old = before.get("grace_period_hours" if field == "grace_hours" else field)
        if old != value:
            lines.append(ACTION_EVENT_FORM_FIELD.format(
                field=field.replace("_", " ").title(), old=old if old is not None else "None",
                new=value))
    if current is None or not before.get("channel_id"):
        lines.append(ACTION_EVENT_FORM_CHANNEL)
    elif before.get("channel_id"):
        lines.append(ACTION_EVENT_FORM_REFRESH.format(channel=f"<#{before['channel_id']}>"))

    async def recheck() -> bool:
        if current is None:
            return True
        return workflow.event_management_state(before["key"]) == current

    async def run() -> CommandOutcome:
        if current is None:
            key = workflow.create_one_time_event(**prepared)
        else:
            key = before["key"]
            if not workflow.update_one_time_event(key, **prepared):
                raise ValueError(ACTION_EVENT_MANAGE_UNAVAILABLE)
        await workflow.force_refresh(context.guild)
        return CommandOutcome("complete", "private",
                              text=embed_text(workflow.build_event_detail_embed(context.guild, key)))

    context.state.command_proposals.append(PreparedAction(
        "create_event_tracker" if current is None else "edit_event_tracker",
        {"event": before.get("key"), "name": prepared["name"]},
        ChangePreview(tuple(lines), recheck, summary=ACTION_EVENT_FORM_LABEL),
        run, action_class=classification,
    ))
    return {"status": "confirmation_required"}


async def _prepare_preset(context: AgentRequestContext,
                          values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("EventStatsCog")
    if workflow is None:
        raise ValueError(ACTION_EVENT_MANAGE_UNAVAILABLE)
    current = workflow.event_management_state(values["event"])
    if current is None or current["event"]["source"] != "preset":
        raise ValueError(ACTION_EVENT_MANAGE_UNAVAILABLE)
    event = current["event"]
    name = values["name"].strip()
    if not name:
        raise ValueError(ACTION_EVENT_MANAGE_UNAVAILABLE)
    grace = values.get("grace_hours")
    if name == event["name"] and (grace is None or grace == event.get("grace_period_hours")):
        return {"status": "no_change"}
    lines = [ACTION_EVENT_FORM_EDIT.format(name=event["name"])]
    if name != event["name"]:
        lines.append(ACTION_EVENT_FORM_FIELD.format(field="Name", old=event["name"], new=name))
    if grace is not None and grace != event.get("grace_period_hours"):
        lines.append(ACTION_EVENT_FORM_FIELD.format(
            field="Grace hours", old=event.get("grace_period_hours"), new=grace))

    async def recheck() -> bool:
        return workflow.event_management_state(event["key"]) == current

    async def run() -> CommandOutcome:
        if not workflow.update_preset_event(event["key"], name=name, grace_hours=grace):
            raise ValueError(ACTION_EVENT_MANAGE_UNAVAILABLE)
        await workflow.force_refresh(context.guild)
        return CommandOutcome("complete", "private", text=embed_text(
            workflow.build_event_detail_embed(context.guild, event["key"])))

    context.state.command_proposals.append(PreparedAction(
        "edit_preset_event", {"event": event["key"]},
        ChangePreview(tuple(lines), recheck, summary=ACTION_EVENT_FORM_LABEL),
        run,
    ))
    return {"status": "confirmation_required"}


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
