"""Confirmed standing requests and watcher controls."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
from datetime import datetime, timezone
import json
import re
from typing import Any
from zoneinfo import ZoneInfo
import discord

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..engine.capability_contract import CapabilityContract
from ..actions.contracts import ActionClass, ChangePreview, PreparedAction
from ..actions.outcomes import ActionOutcome
from ..models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ..wording import (
    ACTION_STANDING_DESTINATION, ACTION_STANDING_MANAGE,
    ACTION_STANDING_MANAGED, ACTION_STANDING_ONCE, ACTION_STANDING_REPEAT,
    ACTION_STANDING_SAVE, ACTION_STANDING_SAVED, ACTION_STANDING_SCOPE,
    ACTION_STANDING_NO_CHANGES, ACTION_STANDING_FIXED, ACTION_STANDING_ACTION,
    ACTION_STANDING_TIME, ACTION_STANDING_WATCHER,
    ACTION_VALUE_YES, ACTION_VALUE_NO,
)
from ..discord_actions.safety import check_post_access, resolve_channel
from ..capabilities import enabled_adapters
from ..commands.bridge import build_command_tools
from ..plan.checker import valid_arguments

from .scope import has_raw_id, validate_scope
from .time_rules import next_occurrences, timezone_name


TOOL_CONTRACTS = {
    'save_standing_rule': CapabilityContract(
        entity_fields=(('destination_channel_id', 'discord_channel'),),
        time_fields=(),
        source_scope='request_context',
        filter_fields=('kind', 'request', 'schedule', 'timezone', 'allowed_actions', 'reads', 'condition', 'repeat', 'replace_id'),
    ),
    'list_standing_rules': CapabilityContract(
        entity_fields=(),
        time_fields=(),
        source_scope='request_context',
        filter_fields=('kind',),
    ),
    'manage_standing_rule': CapabilityContract(
        entity_fields=(),
        time_fields=(),
        source_scope='request_context',
        filter_fields=('kind', 'id', 'operation'),
    ),
}

def _repository(context: AgentRequestContext):
    repository = context.action_repository
    if repository is None:
        raise ValueError("Standing requests are unavailable.")
    return repository


def entity_kind(value: str) -> str:
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


def _allowed_capabilities(
    actions: list[Mapping[str, Any]], context: AgentRequestContext, registry_factory,
) -> None:

    registry = registry_factory()
    command_tools, command_capabilities = build_command_tools(context.bot, enabled_adapters())
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
        if name in command_capabilities:
            entry["action_path"] = command_capabilities[name].adapter.path


def watcher_reads(reads: Any, registry: Mapping[str, RegisteredAgentTool]) -> None:

    if not isinstance(reads, list) or not 1 <= len(reads) <= 8:
        raise ValueError("Choose at least one current or latest lookup.")
    for read in reads:
        if not isinstance(read, Mapping):
            raise ValueError("Choose current or latest lookups.")
        name = read.get("capability")
        arguments = read.get("arguments")
        tool = registry.get(name)
        if (tool is None or tool.action_class is not ActionClass.READ
                or tool.effect is not AgentCapabilityEffect.READ
                or not isinstance(arguments, Mapping)):
            raise ValueError("Watchers use read lookups only.")
        if not valid_arguments(arguments, tool.definition.parameters):
            raise ValueError("Choose valid values for each watcher lookup.")
        contract = tool.contract
        if contract is None:
            raise ValueError("That lookup cannot be watched.")
        if contract.retained_fields or contract.source_scope in (
            "retained_channel_evidence", "retained_attachment", "request_attachment",
        ):
            raise ValueError("Watchers need fresh lookups, not earlier reports or attachments.")
        if contract.time_window is not None:
            raise ValueError("Watchers use current or latest results, not a historical window.")
        if any(field in arguments and arguments[field] not in ("current", "latest")
               for field in contract.time_fields):
            raise ValueError("Watchers use current or latest results only.")


def _validate_watcher(values: Mapping[str, Any], actions, registry_factory) -> None:
    if actions:
        raise ValueError("Watchers send alerts only.")
    watcher_reads(values.get("reads"), registry_factory())
    condition = values.get("condition")
    if not isinstance(condition, str) or not condition.strip():
        raise ValueError("Describe when the watcher should alert.")
    if has_raw_id(condition):
        raise ValueError("Describe the watcher condition with names instead of IDs.")


def _evidence_label(context: AgentRequestContext, name: str, value: Any) -> str | None:
    keys = {name, name + "_id"}
    labels: set[str] = set()

    def visit(item):
        if isinstance(item, Mapping):
            if any(str(item.get(key)) == str(value) for key in keys):
                label = next((item.get(key) for key in ("jump_url", "mention", "name", "title")
                              if isinstance(item.get(key), str) and item[key].strip()), None)
                if label:
                    labels.add(label)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    state = getattr(context, "state", None)
    for record in getattr(state, "evidence", ()):
        try:
            result = json.loads(record).get("result")
            visit(json.loads(result) if isinstance(result, str) else result)
        except (TypeError, ValueError, AttributeError):
            continue
    return next(iter(labels)) if len(labels) == 1 else None


def _field_label(name: str) -> str:
    if name.endswith("_ids"):
        name = name[:-4] + "s"
    else:
        name = name.removesuffix("_id")
    return name.replace("_", " ")


def _fixed_display(context: AgentRequestContext, name: str, value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(_fixed_display(context, name.removesuffix("s"), item)
                         for item in value)
    if isinstance(value, Mapping):
        return ", ".join(f"{_field_label(key)}: {_fixed_display(context, key, item)}"
                         for key, item in value.items())
    if name.endswith("role_id") or name == "role":
        role = context.guild.get_role(value)
        if role is None:
            raise ValueError("Choose a role still in this server.")
        return role.mention
    if name.endswith(("channel_id", "thread_id")) or name in ("channel", "thread"):
        channel = context.guild.get_channel_or_thread(value)
        if channel is None:
            raise ValueError("Choose a channel still in this server.")
        return channel.mention
    if name.endswith(("member_id", "user_id")) or name in ("member", "user"):
        member = context.guild.get_member(value)
        if member is None:
            raise ValueError("Choose a member still in this server.")
        return member.mention
    if isinstance(value, bool):
        return ACTION_VALUE_YES if value else ACTION_VALUE_NO
    label = _evidence_label(context, name, value)
    if label:
        return label
    if name.endswith("_id") or re.fullmatch(r"\d{17,20}", str(value)):
        raise ValueError("Choose a named target for each fixed action value.")
    return str(value)


def _preview_lines(values: Mapping[str, Any], *, kind: str, request: str,
                   zone: str, times: tuple[datetime, ...], channel: Any,
                   actions: list[Mapping[str, Any]], context: AgentRequestContext) -> tuple[str, ...]:
    formatted = ", ".join(item.astimezone(ZoneInfo(zone)).strftime("%d %b %Y %H:%M")
                          for item in times)
    lines = [
        ACTION_STANDING_SAVE.format(kind=kind, request=request),
        ACTION_STANDING_TIME.format(times=formatted, timezone=zone),
        ACTION_STANDING_DESTINATION.format(channel=channel.mention),
        ACTION_STANDING_SCOPE if actions else ACTION_STANDING_NO_CHANGES,
    ]
    for action in actions:
        lines.append(ACTION_STANDING_ACTION.format(
            action=action["scope_text"].rstrip(". "), targets=action["max_targets"],
            target_word="target" if action["max_targets"] == 1 else "targets",
        ))
        fixed = action["fixed_values"]
        if fixed:
            rendered = ", ".join(
                f"{_field_label(name)}: {_fixed_display(context, name, value)}"
                for name, value in fixed.items()
            )
            lines.append(ACTION_STANDING_FIXED.format(values=rendered))
    if kind == "watcher":
        lines.append(ACTION_STANDING_WATCHER.format(
            condition=values["condition"],
            repeat=ACTION_STANDING_REPEAT if values.get("repeat", False)
                   else ACTION_STANDING_ONCE,
        ))
    return tuple(lines)


async def prepare_save(
    context: AgentRequestContext, values: Mapping[str, Any], *, registry_factory,
) -> Mapping[str, Any]:
    repository = _repository(context)
    kind = entity_kind(values["kind"])
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
    actions = [dict(item) if isinstance(item, Mapping) else item for item in actions]
    validate_scope(actions)
    if actions:
        _allowed_capabilities(actions, context, registry_factory)
    if kind == "watcher":
        _validate_watcher(values, actions, registry_factory)
    lines = _preview_lines(values, kind=kind, request=request, zone=zone,
                           times=times, channel=channel, actions=actions,
                           context=context)
    rule = dict(values)
    rule["timezone"] = zone
    rule["allowed_actions"] = actions
    detail_sources = frozenset(context.state.source_channels) | {
        context.source_message.channel.id,
    }
    detail_access = frozenset(context.state.required_access)
    rule["detail_sources"] = sorted(detail_sources)
    rule["detail_access"] = sorted(detail_access)
    identifier = values.get("replace_id")
    version = None
    if identifier:
        current = repository.standing(kind=kind, identifier=identifier,
                                      requester_id=context.member.id)
        if current is None or current["status"] == "cancelled":
            raise ValueError("That saved rule is unavailable.")
        version = current["version"]

    async def recheck() -> bool:
        try:
            refreshed = await resolve_channel(context, values["destination_channel_id"])
            check_post_access(refreshed, context.member, context.guild.me)
            if not identifier:
                return True
            updated = repository.standing(
                kind=kind, identifier=identifier, requester_id=context.member.id,
            )
            return updated is not None and updated["version"] == version
        except (discord.DiscordException, ValueError, RuntimeError, KeyError, TypeError, OSError):
            return False

    async def run() -> ActionOutcome:
        if identifier:
            changed = repository.replace_standing(
                kind=kind, identifier=identifier, requester_id=context.member.id,
                rule=rule, destination_channel_id=channel.id,
                next_at=times[0].timestamp(), expected_version=version,
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
        return ActionOutcome("complete", "public",
                              text=ACTION_STANDING_SAVED.format(kind=kind))

    context.state.proposed_changes.append(PreparedAction(
        "save_standing_rule", rule,
        ChangePreview(lines[1:4], recheck, summary="Save standing rule",
                      details=(lines[0], *lines[4:]),
                      detail_sources=detail_sources, detail_access=detail_access),
        run, action_class=ActionClass.CHANGE,
    ))
    return {"status": "confirmation_required"}


async def list_standing(context: AgentRequestContext,
                        values: Mapping[str, Any]) -> Mapping[str, Any]:
    records = _repository(context).list_standing(requester_id=context.member.id,
                                                  kind=values.get("kind"))
    for record in records:
        context.state.source_channels.update(record["rule"].get("detail_sources", ()))
        context.state.required_access.update(record["rule"].get("detail_access", ()))
    return {"rules": [{
        "id": item.get("request_id") or item.get("watcher_id"),
        "kind": item["kind"], "request": item["rule"]["request"],
        "status": item["status"],
        "next_at": item.get("next_run_at") or item.get("next_check_at"),
    } for item in records]}


async def prepare_manage(context: AgentRequestContext,
                         values: Mapping[str, Any]) -> Mapping[str, Any]:
    repository = _repository(context)
    kind = entity_kind(values["kind"])
    operation = values["operation"]
    if operation not in ("pause", "resume", "cancel"):
        raise ValueError("Choose pause, resume or cancel.")
    identifier = values["id"]
    current = repository.standing(kind=kind, identifier=identifier,
                                  requester_id=context.member.id)
    if current is None or current["status"] not in ("active", "paused"):
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

    async def run() -> ActionOutcome:
        if not repository.set_standing_status(kind=kind, identifier=identifier,
                                              requester_id=context.member.id, status=target):
            raise ValueError("That saved rule changed.")
        return ActionOutcome("complete", "public",
                              text=ACTION_STANDING_MANAGED.format(
                                  kind=kind, result={"resume": "resumed", "pause": "paused",
                                                     "cancel": "cancelled"}[operation]))

    context.state.proposed_changes.append(PreparedAction(
        "manage_standing_rule", dict(values),
        ChangePreview((), recheck, summary="Manage standing rule", details=(line,),
                      detail_sources=frozenset(current["rule"].get("detail_sources", ())),
                      detail_access=frozenset(current["rule"].get("detail_access", ()))),
        run, action_class=ActionClass.CHANGE,
    ))
    return {"status": "confirmation_required"}


def standing_tools(
    registry_factory: Callable[[], dict[str, RegisteredAgentTool]],
) -> tuple[RegisteredAgentTool, ...]:
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
        RegisteredAgentTool(save, partial(prepare_save, registry_factory=registry_factory), AgentCapabilityEffect.COMMAND,
                            ActionClass.CHANGE,
            contract=TOOL_CONTRACTS[save.name],
        ),
        RegisteredAgentTool(listing, list_standing,
            contract=TOOL_CONTRACTS[listing.name],
        ),
        RegisteredAgentTool(manage, prepare_manage, AgentCapabilityEffect.COMMAND,
                            ActionClass.CHANGE,
            contract=TOOL_CONTRACTS[manage.name],
        ),
    )
