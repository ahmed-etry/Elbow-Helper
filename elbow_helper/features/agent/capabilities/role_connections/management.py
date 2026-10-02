"""Confirmed edits to role connection rules."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import discord
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ...access import require_evidence_access
from ...actions.contracts import ActionClass, ChangePreview, PreparedAction
from ...actions.outcomes import CommandOutcome
from ...models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ...wording import (
    ACTION_CONNECTION_MANAGE_LINE,
    ACTION_CONNECTION_MANAGE_TARGET,
    ACTION_CONNECTION_MANAGE_BOARD,
    ACTION_CONNECTION_MANAGE_LABEL,
    ACTION_CONNECTION_MANAGE_DONE,
    ACTION_CONNECTION_MANAGE_OLD_RULE,
    ACTION_CONNECTION_MANAGE_NEW_RULE,
)
from ...discord_actions.safety import check_post_access, check_role, resolve_channel


def role_connection_management_tools() -> tuple[RegisteredAgentTool, ...]:
    condition = {"type": "object", "properties": {
        "has": {"type": "integer", "minimum": 1},
        "not": {"type": "integer", "minimum": 1},
    }, "additionalProperties": False}
    manage = RegisteredAgentTool(AgentToolDefinition(
        name="manage_role_connection",
        description="Create or change a role connection by its desired rule after confirmation.",
        parameters={"type": "object", "properties": {
            "operation": {"type": "string", "enum": ["create", "update"]},
            "connection_id": {"type": "string"},
            "target_role_id": {"type": "integer", "minimum": 1},
            "all": {"type": "array", "items": condition},
            "any": {"type": "array", "items": condition},
            "channel_id": {"type": "integer", "minimum": 1},
        }, "required": ["operation"], "additionalProperties": False},
    ), prepare_role_connection_change, AgentCapabilityEffect.COMMAND,
        ActionClass.CHANGE, True)
    remove = RegisteredAgentTool(AgentToolDefinition(
        name="remove_role_connection",
        description="Remove a role connection after its own confirmation.",
        parameters={"type": "object", "properties": {
            "connection_id": {"type": "string"},
            "channel_id": {"type": "integer", "minimum": 1},
        }, "required": ["connection_id"], "additionalProperties": False},
    ), prepare_remove_role_connection, AgentCapabilityEffect.COMMAND,
        ActionClass.IRREVERSIBLE, True)
    return manage, remove


async def prepare_remove_role_connection(context: AgentRequestContext,
                                         values: Mapping[str, Any]) -> Mapping[str, Any]:
    return await prepare_role_connection_change(context, {**values, "operation": "remove"})


async def prepare_role_connection_change(context: AgentRequestContext,
                                         values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("RoleConnections")
    if workflow is None:
        raise ValueError('That role connection is unavailable.')
    channel = await resolve_channel(
        context, values.get("channel_id") or context.source_message.channel.id)
    if not isinstance(channel, discord.TextChannel):
        raise ValueError('That role connection is unavailable.')
    check_post_access(channel, context.member, context.guild.me)
    operation = values["operation"]
    connection_id = values.get("connection_id")
    before = workflow.role_connection_state(connection_id) if connection_id else None
    if operation == "create":
        if connection_id or "target_role_id" not in values:
            raise ValueError('That role connection is unavailable.')
        connection_id = workflow.new_connection_id()
        after = {"id": connection_id, "target_role_id": values["target_role_id"],
                 "all": list(values.get("all", [])), "any": list(values.get("any", []))}
        if not after["all"] and not after["any"]:
            raise ValueError('That role connection is unavailable.')
    elif operation == "update":
        if before is None:
            raise ValueError('That role connection is unavailable.')
        after = {**before}
        for field in ("target_role_id", "all", "any"):
            if field in values:
                after[field] = values[field]
        if after == before:
            return {"status": "no_change"}
    elif operation == "remove":
        if before is None:
            raise ValueError('That role connection is unavailable.')
        after = None
    else:
        raise ValueError('That role connection is unavailable.')
    candidate = after or before
    role = context.guild.get_role(candidate["target_role_id"])
    check_role(role, context.guild, context.guild.me, {})
    if after is not None and not workflow.connection_change_is_valid(
        after, replacing_id=connection_id if before is not None else None):
        raise ValueError('That role connection is unavailable.')
    lines = [ACTION_CONNECTION_MANAGE_LINE.format(operation=operation, role=role.mention)]
    if before is not None and after is not None and before["target_role_id"] != after["target_role_id"]:
        previous = context.guild.get_role(before["target_role_id"])
        lines.append(ACTION_CONNECTION_MANAGE_TARGET.format(
            old=previous.mention if previous else before["target_role_id"], new=role.mention))
    for list_name in ("all", "any"):
        old_rules = before.get(list_name, []) if before is not None else []
        new_rules = after.get(list_name, []) if after is not None else []
        if old_rules == new_rules and operation == "update":
            continue
        for rule in old_rules:
            kind, role_id = next(iter(rule.items()))
            condition_role = context.guild.get_role(role_id)
            lines.append(ACTION_CONNECTION_MANAGE_OLD_RULE.format(
                group=list_name, kind=kind,
                role=condition_role.mention if condition_role else f"<@&{role_id}>"))
        for rule in new_rules:
            kind, role_id = next(iter(rule.items()))
            condition_role = context.guild.get_role(role_id)
            lines.append(ACTION_CONNECTION_MANAGE_NEW_RULE.format(
                group=list_name, kind=kind,
                role=condition_role.mention if condition_role else f"<@&{role_id}>"))
    lines.append(ACTION_CONNECTION_MANAGE_BOARD.format(channel=channel.mention))

    async def recheck() -> bool:
        current = workflow.role_connection_state(connection_id)
        try:
            check_post_access(channel, context.member, context.guild.me)
            check_role(context.guild.get_role(candidate["target_role_id"]),
                       context.guild, context.guild.me, {})
        except ValueError:
            return False
        return (current == before and (after is None or workflow.connection_change_is_valid(
            after, replacing_id=connection_id if before is not None else None)))

    async def run() -> CommandOutcome:
        if operation == "create":
            workflow.add_connection(after)
        elif operation == "update":
            if not workflow.replace_connection(connection_id, after):
                raise ValueError('That role connection is unavailable.')
        elif not workflow.remove_connection(connection_id):
            raise ValueError('That role connection is unavailable.')
        board = await workflow.refresh_connections_message(channel)
        return CommandOutcome(
            "complete", "private",
            text=ACTION_CONNECTION_MANAGE_DONE.format(channel=channel.mention, url=board.jump_url),
            after={"connection": after},
        )

    context.state.command_proposals.append(PreparedAction(
        "manage_role_connection", {"connection_id": connection_id,
                                   "channel_id": channel.id},
        ChangePreview(tuple(lines), recheck, summary=ACTION_CONNECTION_MANAGE_LABEL,
                      before={"connection": before}),
        run, action_class=ActionClass.IRREVERSIBLE if operation == "remove" else ActionClass.CHANGE,
    ))
    return {"status": "confirmation_required"}


async def prepare_role_connection_undo(context: AgentRequestContext,
                                       log: Mapping[str, Any]) -> PreparedAction:
    workflow = context.bot.get_cog("RoleConnections")
    if workflow is None:
        raise ValueError('That role connection is unavailable.')
    connection_id = log["targets"]["connection_id"]
    channel = await resolve_channel(context, log["targets"]["channel_id"])
    if not isinstance(channel, discord.TextChannel):
        raise ValueError('That role connection is unavailable.')
    check_post_access(channel, context.member, context.guild.me)
    prior = log["before"]["connection"]
    expected = log["after"]["connection"]
    current = workflow.role_connection_state(connection_id)
    if current != expected:
        raise ValueError('That role connection is unavailable.')
    candidate = prior or expected
    role = context.guild.get_role(candidate["target_role_id"])
    check_role(role, context.guild, context.guild.me, {})

    async def recheck() -> bool:
        return workflow.role_connection_state(connection_id) == expected

    async def run() -> CommandOutcome:
        if prior is None:
            if not workflow.remove_connection(connection_id):
                raise ValueError('That role connection is unavailable.')
        elif not workflow.replace_connection(connection_id, prior):
            raise ValueError('That role connection is unavailable.')
        board = await workflow.refresh_connections_message(channel)
        return CommandOutcome("complete", "private",
                              text=ACTION_CONNECTION_MANAGE_DONE.format(
                                  channel=channel.mention, url=board.jump_url),
                              after={"connection": prior})

    return PreparedAction(
        "undo_role_connection", {"connection_id": connection_id, "channel_id": channel.id},
        ChangePreview((ACTION_CONNECTION_MANAGE_LINE.format(
            operation="Restore" if prior is not None else "Remove",
            role=role.mention), ACTION_CONNECTION_MANAGE_BOARD.format(channel=channel.mention)),
            recheck, summary=ACTION_CONNECTION_MANAGE_LABEL,
            before={"connection": expected}),
        run,
    )
