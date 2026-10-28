"""Confirmed Discord role changes through guarded per-member actions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..engine.capability_contract import CapabilityContract
from ..access import require_evidence_access
from ..actions.contracts import ActionClass, ChangePreview, PreparedAction, audit_reason
from ..actions.outcomes import ActionOutcome
from ..models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ..wording import (
    ACTION_ROLE_ADD_LABEL, ACTION_ROLE_ADD_LINE,
    ACTION_ROLE_REMOVE_LABEL, ACTION_ROLE_REMOVE_LINE,
    ACTION_ROLE_UNDO_LABEL, ACTION_UNDO_CHANGED,
)
from .safety import (
    DiscordActionRefused, check_member, check_member_role_ownership, check_raw_role,
    managed_role_commands, resolve_member,
)


def discord_role_tools() -> tuple[RegisteredAgentTool, ...]:
    schema = {"type": "object", "properties": {
        "role_id": {"type": "integer", "minimum": 1},
        "role_name": {"type": "string", "minLength": 1, "maxLength": 100},
        "member_ids": {"type": "array", "items": {"type": "integer", "minimum": 1},
                       "minItems": 1, "maxItems": 25, "uniqueItems": True},
    }, "required": ["member_ids"], "anyOf": [
        {"required": ["role_id"]}, {"required": ["role_name"]}], "additionalProperties": False}
    return (
        RegisteredAgentTool(AgentToolDefinition(
            name="read_discord_role_change_preview",
            description="Read current roles and a role delta by exact name or ID, without preparing or executing an action.",
            parameters={"type": "object", "properties": {
                "role_name": {"type": "string", "minLength": 1, "maxLength": 100},
                "role_id": {"type": "integer", "minimum": 1},
                "member_ids": schema["properties"]["member_ids"],
                "operation": {"type": "string", "enum": ["add", "remove"]}},
                "required": ["member_ids", "operation"], "additionalProperties": False},
            usage_note='Read-only eligibility and exact delta; use this for inspection. To prepare a confirmation preview use add_discord_roles or remove_discord_roles directly.',
        ), read_role_change_preview, contract=CapabilityContract(
            entity_fields=(("role_id", "discord_role"), ("member_ids", "discord_member_set"))),
        returns="role_id,role_name,members:[{member_id,current_role_ids,add_role_ids,remove_role_ids,allowed,refusal}]"),
        RegisteredAgentTool(AgentToolDefinition(
            name="add_discord_roles",
            description="Prepare a confirmation preview to add one Discord role by exact name or ID; never applies it.",
            parameters=schema,
            usage_note='Supply role_name or role_id plus member_ids. Resolves the role, checks eligibility and existing membership, then queues only actual changes for confirmation.',
        ), prepare_add_roles, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
            contract=CapabilityContract(
                entity_fields=(("role_id", "discord_role"), ("member_ids", "discord_member_set")),
                source_scope="request_context",
                filter_fields=("role_name",),
            ),
        ),
        RegisteredAgentTool(AgentToolDefinition(
            name="remove_discord_roles",
            description="Prepare a confirmation preview to remove one Discord role by exact name or ID; never applies it.",
            parameters=schema,
            usage_note='Supply role_name or role_id plus member_ids. Resolves the role, checks eligibility and existing membership, then queues only actual changes for confirmation.',
        ), prepare_remove_roles, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
            contract=CapabilityContract(
                entity_fields=(("role_id", "discord_role"), ("member_ids", "discord_member_set")),
                source_scope="request_context",
                filter_fields=("role_name",),
            ),
        ),
    )


def selected_role(context, arguments):
    """Resolve an exact guild-owned identifier; never infer a role from request prose."""
    if arguments.get('role_id'):
        role = context.guild.get_role(arguments['role_id'])
        roles = [role] if role is not None else []
    else:
        roles = [role for role in context.guild.roles
                 if role.name.casefold() == str(arguments.get('role_name', '')).casefold()]
    if len(roles) != 1 or arguments.get('role_name') and roles[0].name.casefold() != arguments['role_name'].casefold():
        raise DiscordActionRefused('Choose one exact guild role name or ID.')
    return roles[0]


def role_change_access(context):
    """Expose live requester authority before the model chooses an action."""
    current = context.guild.get_member(context.member.id)
    return {'requester_allowed': bool(current and getattr(
                getattr(current, 'guild_permissions', None), 'manage_roles', False)),
            'required_permission': 'Manage Roles',
            'operator_roles': [{'role_id': role.id, 'name': role.name}
                for role in getattr(context.guild, 'roles', ())
                if getattr(getattr(role, 'permissions', None), 'manage_roles', False)
                or getattr(getattr(role, 'permissions', None), 'administrator', False)]}


async def read_role_change_preview(context, arguments):
    """Read-only role arithmetic with the same eligibility checks as preparation."""
    await require_evidence_access(context)
    try:
        role = selected_role(context, arguments)
    except DiscordActionRefused as error:
        return {'error': str(error), 'cause': 'needs_input'}
    owners = await managed_role_commands(context)
    rows = []
    for identifier in arguments['member_ids']:
        member = await resolve_member(context.guild, identifier)
        current = sorted(item.id for item in member.roles)
        change = (role.id not in current) == (arguments['operation'] == 'add')
        refusal = None
        try:
            check_raw_role(role, context.guild, context.guild.me, owners, requester=context.member)
            check_member(member, context.guild.me, requester=context.member, guild=context.guild)
            if change:
                check_member_role_ownership(context, role, member)
        except DiscordActionRefused as error:
            refusal = str(error)
        rows.append({'member_id': identifier, 'member_name': member.display_name, 'current_role_ids': current,
            'add_role_ids': [role.id] if change and arguments['operation'] == 'add' else [],
            'remove_role_ids': [role.id] if change and arguments['operation'] == 'remove' else [],
            'allowed': refusal is None, 'refusal': refusal})
    await require_evidence_access(context)
    return {'role_id': role.id, 'role_name': role.name, 'members': rows, 'preview_only': True,
            'role_change_access': role_change_access(context),
            'status': 'eligibility_checked', 'change_attempted': False, 'confirmation_prepared': False}


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
        role = selected_role(context, arguments)
        owners = await managed_role_commands(context)
        check_raw_role(role, context.guild, context.guild.me, owners, requester=context.member)
        selected = []
        members = []
        for member_id in arguments["member_ids"]:
            member = await resolve_member(context.guild, member_id)
            check_member(member, context.guild.me, requester=context.member, guild=context.guild)
            members.append({"member_id": member.id, "display_name": member.display_name,
                            "has_role": role in member.roles})
            if (role in member.roles) != add:
                check_member_role_ownership(context, role, member)
                selected.append(member)
    except DiscordActionRefused as error:
        return {"error": str(error), 'cause': 'ineligible',
                'role_change_access': role_change_access(context),
                'change_attempted': False, 'confirmation_prepared': False}
    await require_evidence_access(context)
    for member in selected:
        context.state.proposed_changes.append(_role_action(
            context, role.id, member.id, add=add,
            before=not add, label_role=role.mention, label_member=member.mention,
        ))
    return {"status": "confirmation_required" if selected else "no_change",
            "role_id": role.id, "role_name": role.name, "members": members,
            "requested_has_role": add}


def _role_action(
    context: AgentRequestContext, role_id: int, member_id: int, *, add: bool,
    before: bool, label_role: str, label_member: str,
    undo: bool = False, changed: bool = False,
) -> PreparedAction:
    line_template = ACTION_ROLE_ADD_LINE if add else ACTION_ROLE_REMOVE_LINE
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
        check_member(member, context.guild.me, requester=context.member, guild=context.guild)
        check_member_role_ownership(context, role, member)
        return role, member

    async def recheck() -> bool:
        try:
            role, member = await targets()
        except DiscordActionRefused:
            return False
        return (role in member.roles) == before

    async def run() -> ActionOutcome:
        role, member = await targets()
        if add:
            await member.add_roles(role, reason=audit_reason(context.member))
        else:
            await member.remove_roles(role, reason=audit_reason(context.member))
        return ActionOutcome(
            "complete",
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
    check_member(member, context.guild.me, requester=context.member, guild=context.guild)
    check_member_role_ownership(context, role, member)
    expected = log["after"]["has_role"]
    before = log["before"]["has_role"]
    return _role_action(
        context, role.id, member.id, add=before, before=expected,
        label_role=role.mention, label_member=member.mention,
        undo=True, changed=(role in member.roles) != expected,
    )


UNDO_HANDLERS = {
    'add_discord_roles': prepare_role_undo,
    'remove_discord_roles': prepare_role_undo,
    'undo_discord_role': prepare_role_undo,
}
