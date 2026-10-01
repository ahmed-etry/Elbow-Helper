"""Confirmed standing requests and watcher controls."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo
import discord

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..actions.contracts import ActionClass, ChangePreview, PreparedAction
from ..commands.outcomes import CommandOutcome
from ..models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ..wording import (
    ACTION_STANDING_DESTINATION, ACTION_STANDING_MANAGE,
    ACTION_STANDING_MANAGED, ACTION_STANDING_ONCE, ACTION_STANDING_REPEAT,
    ACTION_STANDING_SAVE, ACTION_STANDING_SAVED, ACTION_STANDING_SCOPE,
    ACTION_STANDING_NO_CHANGES,
    ACTION_STANDING_TIME, ACTION_STANDING_WATCHER,
)
from ..tools.discord_safety import check_post_access, resolve_channel
from .scope import validate_scope
from .time_rules import next_occurrences, timezone_name


def _repository(context: AgentRequestContext):
    repository = context.action_repository
    if repository is None:
        raise ValueError("Standing requests are unavailable.")
    return repository


def _kind(value: str) -> str:
    if value not in ("request", "watcher"):
        raise ValueError("Choose a saved request or watcher.")
    return value


def _schedule(values: Mapping[str, Any], *, watcher: bool, zone: str) -> tuple[datetime, ...]:
    rule = values.get("schedule")
    if not isinstance(rule, Mapping):
        raise ValueError("Choose a run time.")
    times = next_occurrences(rule, after=datetime.now(timezone.utc), watcher=watcher)
    if not times:
        raise ValueError("Choose a future run time.")
    return times


def _allowed_capabilities(actions: list[Mapping[str, Any]], context: AgentRequestContext) -> None:
    from ..tools import build_agent_tools
    from ..commands.adapters import enabled_adapters
    from ..commands.bridge import build_command_tools

    registry = build_agent_tools()
    command_tools, _ = build_command_tools(context.bot, enabled_adapters())
    registry.update(command_tools)
    for entry in actions:
        name = entry["capability"]
        selected = registry.get(name)
        if name in {"save_standing_rule", "manage_standing_rule"} or selected is None or selected.action_class not in (ActionClass.CHANGE,
                                                                ActionClass.IRREVERSIBLE):
            raise ValueError("Choose an available change for this request.")
        properties = selected.definition.parameters.get("properties", {})
        named = set(entry["fixed_values"]) | set(entry["variable_fields"])
        required = set(selected.definition.parameters.get("required", ()))
        if not named <= set(properties) or not required <= named:
            raise ValueError("Set the fixed and changing values for each required action option.")


def _watcher_reads(reads: Any) -> None:
    from ..tools import build_agent_tools
    from ..capabilities import CONTRACTS

    registry = build_agent_tools()
    if not isinstance(reads, list) or not reads:
        raise ValueError("Choose at least one current or latest lookup.")
    for read in reads:
        if not isinstance(read, Mapping):
            raise ValueError("Choose current or latest lookups.")
        name = read.get("capability")
        arguments = read.get("arguments")
        tool = registry.get(name)
        if tool is None or tool.action_class is not ActionClass.READ or not isinstance(arguments, Mapping):
            raise ValueError("Watchers use read lookups only.")
        contract = CONTRACTS.get(name)
        if contract is None:
            raise ValueError("That lookup cannot be watched.")
        if any(field in arguments and arguments[field] not in ("current", "latest")
               for field in contract.time_fields):
            raise ValueError("Watchers use current or latest results only.")


def _preview_lines(values: Mapping[str, Any], *, kind: str, request: str,
                   zone: str, times: tuple[datetime, ...], channel: Any,
                   actions: list[Mapping[str, Any]]) -> tuple[str, ...]:
    formatted = ", ".join(item.astimezone(ZoneInfo(zone)).strftime("%d %b %Y %H:%M")
                          for item in times)
    lines = [
        ACTION_STANDING_SAVE.format(kind=kind, request=request),
        ACTION_STANDING_TIME.format(times=formatted, timezone=zone),
        ACTION_STANDING_DESTINATION.format(channel=channel.mention),
        (ACTION_STANDING_SCOPE.format(
            actions="; ".join(item["scope_text"] for item in actions),
            targets=max(item["max_targets"] for item in actions),
        ) if actions else ACTION_STANDING_NO_CHANGES),
    ]
    if kind == "watcher":
        lines.append(ACTION_STANDING_WATCHER.format(
            condition=values["condition"],
            repeat=ACTION_STANDING_REPEAT if values.get("repeat", False)
                   else ACTION_STANDING_ONCE,
        ))
    return tuple(lines)


async def prepare_save(context: AgentRequestContext,
                       values: Mapping[str, Any]) -> Mapping[str, Any]:
    repository = _repository(context)
    kind = _kind(values["kind"])
    request = str(values["request"]).strip()
    if not request or len(request) > 4000:
        raise ValueError("Describe the request in fewer than 4,000 characters.")
    zone = values.get("timezone") or repository.member_timezone(context.member.id)
    if not zone:
        return {"missing_timezone": True}
    zone = timezone_name(zone)
    times = _schedule(values, watcher=kind == "watcher", zone=zone)
    channel = await resolve_channel(context, values["destination_channel_id"])
    check_post_access(channel, context.member, context.guild.me)
    actions = values.get("allowed_actions", [])
    if not isinstance(actions, list):
        raise ValueError("Choose the changes this request may make.")
    validate_scope(actions)
    if actions:
        _allowed_capabilities(actions, context)
    if kind == "watcher":
        if actions:
            raise ValueError("Watchers send alerts only.")
        _watcher_reads(values.get("reads"))
        if not str(values.get("condition", "")).strip():
            raise ValueError("Describe when the watcher should alert.")
    lines = _preview_lines(values, kind=kind, request=request, zone=zone,
                           times=times, channel=channel, actions=actions)
    rule = dict(values)
    rule["timezone"] = zone
    rule["allowed_actions"] = actions
    identifier = values.get("replace_id")
    if identifier:
        current = repository.standing(kind=kind, identifier=identifier,
                                      requester_id=context.member.id)
        if current is None:
            raise ValueError("That saved rule is unavailable.")

    async def recheck() -> bool:
        try:
            refreshed = await resolve_channel(context, values["destination_channel_id"])
            check_post_access(refreshed, context.member, context.guild.me)
            return not identifier or repository.standing(
                kind=kind, identifier=identifier, requester_id=context.member.id,
            ) is not None
        except (discord.DiscordException, ValueError, RuntimeError, KeyError, TypeError, OSError):
            return False

    async def run() -> CommandOutcome:
        if identifier:
            changed = repository.replace_standing(
                kind=kind, identifier=identifier, requester_id=context.member.id,
                rule=rule, destination_channel_id=channel.id,
                next_at=times[0].timestamp(),
            )
            if not changed:
                raise ValueError("That saved rule changed.")
        else:
            repository.create_standing(
                kind=kind, guild_id=context.guild.id, requester_id=context.member.id,
                destination_channel_id=channel.id, rule=rule,
                next_at=times[0].timestamp(),
            )
        repository.set_member_timezone(context.member.id, zone)
        return CommandOutcome("complete", "public",
                              text=ACTION_STANDING_SAVED.format(kind=kind))

    context.state.command_proposals.append(PreparedAction(
        "save_standing_rule", rule,
        ChangePreview(lines, recheck, summary="Save standing rule"),
        run, action_class=ActionClass.CHANGE,
    ))
    return {"status": "confirmation_required"}


async def list_standing(context: AgentRequestContext,
                        values: Mapping[str, Any]) -> Mapping[str, Any]:
    records = _repository(context).list_standing(requester_id=context.member.id,
                                                  kind=values.get("kind"))
    return {"rules": [{
        "id": item.get("request_id") or item.get("watcher_id"),
        "kind": item["kind"], "request": item["rule"]["request"],
        "status": item["status"],
        "next_at": item.get("next_run_at") or item.get("next_check_at"),
    } for item in records]}


async def prepare_manage(context: AgentRequestContext,
                         values: Mapping[str, Any]) -> Mapping[str, Any]:
    repository = _repository(context)
    kind = _kind(values["kind"])
    operation = values["operation"]
    if operation not in ("pause", "resume", "cancel"):
        raise ValueError("Choose pause, resume or cancel.")
    identifier = values["id"]
    current = repository.standing(kind=kind, identifier=identifier,
                                  requester_id=context.member.id)
    if current is None or current["status"] == "cancelled":
        raise ValueError("That saved rule is unavailable.")
    target = "active" if operation == "resume" else ("paused" if operation == "pause" else "cancelled")
    line = ACTION_STANDING_MANAGE.format(operation=operation.capitalize(), kind=kind,
                                         request=current["rule"]["request"])

    async def recheck() -> bool:
        refreshed = repository.standing(kind=kind, identifier=identifier,
                                        requester_id=context.member.id)
        applicable = ("paused",) if operation == "resume" else (
            ("active",) if operation == "pause" else ("active", "paused")
        )
        return refreshed is not None and refreshed["status"] in applicable

    async def run() -> CommandOutcome:
        if not repository.set_standing_status(kind=kind, identifier=identifier,
                                              requester_id=context.member.id, status=target):
            raise ValueError("That saved rule changed.")
        return CommandOutcome("complete", "public",
                              text=ACTION_STANDING_MANAGED.format(kind=kind, status=target))

    context.state.command_proposals.append(PreparedAction(
        "manage_standing_rule", dict(values),
        ChangePreview((line,), recheck, summary="Manage standing rule"),
        run, action_class=ActionClass.CHANGE,
    ))
    return {"status": "confirmation_required"}


def standing_tools() -> tuple[RegisteredAgentTool, ...]:
    save = AgentToolDefinition(
        "save_standing_rule",
        "Save or change a scheduled request or watcher after confirmation. Give exact UTC once/interval times or weekly/monthly local times, destination, fixed action values, changing target/content fields and target limits. Name every fixed value in scope_text. Watchers need current/latest reads, condition and repeat choice.",
        {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["request", "watcher"]},
            "request": {"type": "string"},
            "schedule": {"type": "object", "properties": {
                "kind": {"type": "string", "enum": ["once", "interval", "weekly", "monthly"]},
                "at_utc": {"type": "string"}, "anchor_utc": {"type": "string"},
                "seconds": {"type": "integer"}, "timezone": {"type": "string"},
                "time": {"type": "string"}, "days": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 31}},
            }, "anyOf": [
                {"properties": {"kind": {"enum": ["weekly"]},
                                "days": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 6}}}},
                {"properties": {"kind": {"enum": ["monthly"]},
                                "days": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 31}}}},
                {"properties": {"kind": {"enum": ["once", "interval"]}}},
            ]},
            "timezone": {"type": "string"},
            "destination_channel_id": {"type": "integer"},
            "allowed_actions": {"type": "array", "items": {"type": "object", "properties": {
                "capability": {"type": "string"}, "fixed_values": {"type": "object"},
                "variable_fields": {"type": "array", "items": {"type": "string"}},
                "max_targets": {"type": "integer"}, "scope_text": {"type": "string"},
            }}},
            "reads": {"type": "array", "items": {"type": "object", "properties": {
                "capability": {"type": "string"}, "arguments": {"type": "object"},
            }}},
            "condition": {"type": "string"},
            "repeat": {"type": "boolean"},
            "replace_id": {"type": "string"},
        }, "required": ["kind", "request", "schedule", "destination_channel_id"]},
    )
    listing = AgentToolDefinition(
        "list_standing_rules", "List this member's saved requests and watchers.",
        {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["request", "watcher"]},
        }},
    )
    manage = AgentToolDefinition(
        "manage_standing_rule", "Pause, resume or cancel a member's saved request or watcher after confirmation.",
        {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["request", "watcher"]},
            "id": {"type": "string"},
            "operation": {"type": "string", "enum": ["pause", "resume", "cancel"]},
        }, "required": ["kind", "id", "operation"]},
    )
    return (
        RegisteredAgentTool(save, prepare_save, AgentCapabilityEffect.COMMAND,
                            ActionClass.CHANGE, True),
        RegisteredAgentTool(listing, list_standing),
        RegisteredAgentTool(manage, prepare_manage, AgentCapabilityEffect.COMMAND,
                            ActionClass.CHANGE, True),
    )
