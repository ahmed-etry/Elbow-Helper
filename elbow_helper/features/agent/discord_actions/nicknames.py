"""Confirmed nickname changes with fresh member checks."""

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
    ACTION_NICKNAME_LABEL, ACTION_NICKNAME_LINE,
    ACTION_NICKNAME_RESET_LINE, ACTION_UNDO_CHANGED,
)
from .safety import DiscordActionRefused, check_raw_nickname, resolve_member


def discord_nickname_tools() -> tuple[RegisteredAgentTool, ...]:
    return (RegisteredAgentTool(AgentToolDefinition(
        name="change_discord_nickname",
        description="Set or reset a member's nickname after confirmation. Omit nickname to reset it.",
        parameters={"type": "object", "properties": {
            "member_id": {"type": "integer", "minimum": 1},
            "nickname": {"type": "string", "minLength": 1, "maxLength": 32},
        }, "required": ["member_id"], "additionalProperties": False},
    ), prepare_nickname, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
        contract=CapabilityContract(
            entity_fields=(("member_id", "discord_member"),),
            time_fields=(),
            source_scope="request_context",
            filter_fields=("nickname",),
        ),
            ),)


def _nickname_action(context: AgentRequestContext, member_id: int,
                     *, before: str | None, after: str | None,
                     label: str, undo: bool = False,
                     changed: bool = False) -> PreparedAction:
    async def current() -> Any:
        member = await resolve_member(context.guild, member_id, fresh=True)
        check_raw_nickname(member, context.guild.me, requester=context.member, guild=context.guild)
        return member

    async def recheck() -> bool:
        try:
            member = await current()
        except DiscordActionRefused:
            return False
        return member.nick == before

    async def run() -> ActionOutcome:
        member = await current()
        await member.edit(nick=after, reason=audit_reason(context.member))
        return ActionOutcome("complete", after={"nickname": after},
                              result={"member_id": member_id})

    async def verify() -> bool:
        member = await current()
        return member.nick == after

    line = (ACTION_NICKNAME_LINE.format(member=label, nickname=after)
            if after is not None else ACTION_NICKNAME_RESET_LINE.format(member=label))
    return PreparedAction(
        "undo_discord_nickname" if undo else "change_discord_nickname",
        {"member_id": member_id, "nickname": after},
        ChangePreview((line, *((ACTION_UNDO_CHANGED,) if changed else ())),
                      recheck, summary=ACTION_NICKNAME_LABEL,
                      before={"nickname": before}),
        run, verify=verify, permission="Manage Nicknames",
    )


async def prepare_nickname(context: AgentRequestContext,
                           arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    try:
        member = await resolve_member(context.guild, arguments["member_id"])
        check_raw_nickname(member, context.guild.me, requester=context.member, guild=context.guild)
    except DiscordActionRefused as error:
        return {"error": str(error)}
    after = arguments.get("nickname")
    if member.nick == after:
        return {"status": "no_change"}
    context.state.proposed_changes.append(_nickname_action(
        context, member.id, before=member.nick, after=after, label=member.mention,
    ))
    return {"status": "confirmation_required"}


async def prepare_nickname_undo(context: AgentRequestContext,
                                log: Mapping[str, Any]) -> PreparedAction:
    member_id = log["targets"]["member_id"]
    member = await resolve_member(context.guild, member_id)
    check_raw_nickname(member, context.guild.me, requester=context.member, guild=context.guild)
    expected = log["after"]["nickname"]
    return _nickname_action(
        context, member_id, before=expected, after=log["before"]["nickname"],
        label=member.mention, undo=True, changed=member.nick != expected,
    )


UNDO_HANDLERS = {
    'change_discord_nickname': prepare_nickname_undo,
    'undo_discord_nickname': prepare_nickname_undo,
}
