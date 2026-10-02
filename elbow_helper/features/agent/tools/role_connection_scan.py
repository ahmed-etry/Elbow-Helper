"""Preview every role assignment from the role connections scan."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..actions.contracts import ActionClass, ChangePreview, PreparedAction
from ..actions.outcomes import CommandOutcome
from ..models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ..wording import (
    ACTION_ROLE_ADD_LINE,
    ACTION_ROLE_REMOVE_LINE,
    ACTION_ROLE_SCAN_LABEL,
)
from ..discord_actions.safety import check_member, check_role, resolve_member


def role_connection_scan_tools() -> tuple[RegisteredAgentTool, ...]:
    return (RegisteredAgentTool(AgentToolDefinition(
        name="apply_role_connections",
        description="Apply all current role connection rules after previewing each member change.",
        parameters={"type": "object", "properties": {},
                    "required": [], "additionalProperties": False},
    ), prepare_role_connection_scan, AgentCapabilityEffect.COMMAND,
        ActionClass.CHANGE, True),)


async def prepare_role_connection_scan(context: AgentRequestContext,
                                       values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("RoleConnections")
    if workflow is None:
        raise ValueError('Role connections are unavailable.')
    signature = workflow.connections_board_signature()
    plan = await workflow.role_connection_scan_plan(context.guild)
    changes = [(member, role, add) for member, actions in plan
               for role, add in actions]
    if not changes:
        return {"status": "no_change", "prepared_count": 0}
    for member, role, _ in changes:
        check_member(member, context.guild.me)
        check_role(role, context.guild, context.guild.me, {})
    await require_evidence_access(context)
    for member, role, add in changes:
        context.state.command_proposals.append(_scan_action(
            context, workflow, member.id, role.id, add,
            member_label=member.mention, role_label=role.mention,
            signature=signature,
        ))
    return {"status": "confirmation_required", "prepared_count": len(changes)}


def _scan_action(context: AgentRequestContext, workflow: Any,
                 member_id: int, role_id: int, add: bool, *,
                 member_label: str, role_label: str, signature: str,
                 undo: bool = False) -> PreparedAction:
    async def targets():
        member = await resolve_member(context.guild, member_id, fresh=True)
        role = context.guild.get_role(role_id)
        check_member(member, context.guild.me)
        check_role(role, context.guild, context.guild.me, {})
        return member, role

    async def recheck() -> bool:
        try:
            member, role = await targets()
        except ValueError:
            return False
        return (workflow.connections_board_signature() == signature
                and (role in member.roles) != add)

    async def run() -> CommandOutcome:
        member, role = await targets()
        if not await workflow.apply_role_connection_change(member, role, add=add):
            raise ValueError('Role connections are unavailable.')
        return CommandOutcome("complete", after={"has_role": add})

    line = (ACTION_ROLE_ADD_LINE if add else ACTION_ROLE_REMOVE_LINE).format(
        role=role_label, member=member_label)
    return PreparedAction(
        "undo_role_connection_scan" if undo else "apply_role_connections",
        {"member_id": member_id, "role_id": role_id, "add": add},
        ChangePreview((line,), recheck, summary=ACTION_ROLE_SCAN_LABEL,
                      before={"has_role": not add}),
        run,
    )


async def prepare_role_connection_scan_undo(context: AgentRequestContext,
                                            log: Mapping[str, Any]) -> PreparedAction:
    workflow = context.bot.get_cog("RoleConnections")
    if workflow is None:
        raise ValueError('Role connections are unavailable.')
    values = log["targets"]
    member = await resolve_member(context.guild, values["member_id"])
    role = context.guild.get_role(values["role_id"])
    check_member(member, context.guild.me)
    check_role(role, context.guild, context.guild.me, {})
    return _scan_action(
        context, workflow, member.id, role.id,
        log["before"]["has_role"],
        member_label=member.mention, role_label=role.mention,
        signature=workflow.connections_board_signature(), undo=True,
    )
