"""Confirmed Discord role changes through guarded per-member actions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..actions.contracts import ActionClass, ChangePreview, PreparedAction, audit_reason
from ..commands.outcomes import CommandOutcome
from ..models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ..wording import (
    ACTION_ROLE_ADD_DONE, ACTION_ROLE_ADD_LABEL, ACTION_ROLE_ADD_LINE,
    ACTION_ROLE_REMOVE_DONE, ACTION_ROLE_REMOVE_LABEL, ACTION_ROLE_REMOVE_LINE,
    ACTION_ROLE_UNDO_LABEL, ACTION_UNDO_CHANGED,
)
from .discord_safety import (
    DiscordActionRefused, check_member, check_raw_role, managed_role_commands,
    resolve_member,
)


def discord_role_tools() -> tuple[RegisteredAgentTool, ...]:
    schema = {"type": "object", "properties": {
        "role_id": {"type": "integer", "minimum": 1},
        "member_ids": {"type": "array", "items": {"type": "integer", "minimum": 1},
                       "minItems": 1, "maxItems": 25, "uniqueItems": True},
    }, "required": ["role_id", "member_ids"], "additionalProperties": False}
    return (
        RegisteredAgentTool(AgentToolDefinition(
            name="add_discord_roles",
            description="Add one safe Discord role to the selected members after confirmation.",
            parameters=schema,
        ), prepare_add_roles, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE, True),
        RegisteredAgentTool(AgentToolDefinition(
            name="remove_discord_roles",
            description="Remove one safe Discord role from the selected members after confirmation.",
            parameters=schema,
        ), prepare_remove_roles, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE, True),
    )


async def prepare_add_roles(context: AgentRequestContext,
                            arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    return await _prepare_roles(context, arguments, add=True)


async def prepare_remove_roles(context: AgentRequestContext,
                               arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    return await _prepare_roles(context, arguments, add=False)


async def _prepare_roles(context: AgentRequestContext, arguments: Mapping[str, Any],
                         *, add: bool) -> Mapping[str, Any]:
    await require_evidence_access(context)
    try:
        role = context.guild.get_role(arguments["role_id"])
        owners = await managed_role_commands(context)
        check_raw_role(role, context.guild, context.guild.me, owners, requester=context.member)
        selected = []
        for member_id in arguments["member_ids"]:
            member = await resolve_member(context.guild, member_id)
            check_member(member, context.guild.me)
            if (role in member.roles) != add:
                selected.append(member)
    except DiscordActionRefused as error:
        return {"error": str(error), "prepared_count": 0}
    await require_evidence_access(context)
    for member in selected:
        context.state.command_proposals.append(_role_action(
            context, role.id, member.id, add=add,
            before=not add, label_role=role.mention, label_member=member.mention,
        ))
    return {"status": "confirmation_required" if selected else "no_change",
            "prepared_count": len(selected)}


def _role_action(
    context: AgentRequestContext, role_id: int, member_id: int, *, add: bool,
    before: bool, label_role: str, label_member: str,
    undo: bool = False, changed: bool = False,
) -> PreparedAction:
    line_template = ACTION_ROLE_ADD_LINE if add else ACTION_ROLE_REMOVE_LINE
    done_template = ACTION_ROLE_ADD_DONE if add else ACTION_ROLE_REMOVE_DONE
    summary = ACTION_ROLE_UNDO_LABEL if undo else (
        ACTION_ROLE_ADD_LABEL if add else ACTION_ROLE_REMOVE_LABEL
    )
    path = "undo_discord_role" if undo else (
        "add_discord_roles" if add else "remove_discord_roles"
    )

    async def targets() -> tuple[Any, Any]:
        role = context.guild.get_role(role_id)
        owners = await managed_role_commands(context)
        check_raw_role(role, context.guild, context.guild.me, owners, requester=context.member)
        member = await resolve_member(context.guild, member_id, fresh=True)
        check_member(member, context.guild.me)
        return role, member

    async def recheck() -> bool:
        try:
            role, member = await targets()
        except DiscordActionRefused:
            return False
        return (role in member.roles) == before

    async def run() -> CommandOutcome:
        role, member = await targets()
        if add:
            await member.add_roles(role, reason=audit_reason(context.member))
        else:
            await member.remove_roles(role, reason=audit_reason(context.member))
        return CommandOutcome(
            "complete", text=done_template.format(role=label_role, member=label_member),
            after={"has_role": add},
        )

    async def verify() -> bool:
        role = context.guild.get_role(role_id)
        if role is None:
            return False
        member = await resolve_member(context.guild, member_id, fresh=True)
        return (role in member.roles) == add

    line = line_template.format(role=label_role, member=label_member)
    return PreparedAction(
        path, {"role_id": role_id, "member_id": member_id},
        ChangePreview((line, *((ACTION_UNDO_CHANGED,) if changed else ())),
                      recheck, summary=summary,
                      before={"has_role": before}),
        run, verify=verify, permission="Manage Roles",
    )


async def prepare_role_undo(context: AgentRequestContext,
                            log: Mapping[str, Any]) -> PreparedAction:
    values = log["targets"]
    role = context.guild.get_role(values["role_id"])
    member = await resolve_member(context.guild, values["member_id"])
    owners = await managed_role_commands(context)
    check_raw_role(role, context.guild, context.guild.me, owners, requester=context.member)
    check_member(member, context.guild.me)
    expected = log["after"]["has_role"]
    before = log["before"]["has_role"]
    return _role_action(
        context, role.id, member.id, add=before, before=expected,
        label_role=role.mention, label_member=member.mention,
        undo=True, changed=(role in member.roles) != expected,
    )
