"""Confirmed reopening of feature tickets."""

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
    ACTION_TICKET_REOPEN_LINE, ACTION_TICKET_REOPEN_ACCESS,
    ACTION_TICKET_REOPEN_LABEL, ACTION_TICKET_REOPEN_UNAVAILABLE,
)
from .discord_safety import check_member, check_post_access, resolve_channel


def ticket_reopen_tools() -> tuple[RegisteredAgentTool, ...]:
    return (RegisteredAgentTool(AgentToolDefinition(
        name="reopen_support_ticket",
        description="Restore the owner's messaging access to a closed support ticket after confirmation.",
        parameters={"type": "object", "properties": {
            "channel_id": {"type": "integer", "minimum": 1},
        }, "additionalProperties": False},
    ), prepare_support_reopen, AgentCapabilityEffect.COMMAND,
        ActionClass.CHANGE, True),)


async def prepare_support_reopen(context: AgentRequestContext,
                                 values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("SupportActions")
    if workflow is None or not workflow.can_manage_ticket_controls(context.member):
        raise ValueError(ACTION_TICKET_REOPEN_UNAVAILABLE)
    channel = await resolve_channel(context, values.get("channel_id") or context.source_message.channel.id)
    if not isinstance(channel, discord.TextChannel):
        raise ValueError(ACTION_TICKET_REOPEN_UNAVAILABLE)
    check_post_access(channel, context.member, context.guild.me)
    owner = workflow.support_reopen_state(context.guild, channel)
    check_member(owner, context.guild.me)
    before = channel.overwrites_for(owner).send_messages
    if before is True:
        return {"status": "no_change"}
    lines = (
        ACTION_TICKET_REOPEN_LINE.format(channel=channel.mention),
        ACTION_TICKET_REOPEN_ACCESS.format(member=owner.mention,
                                          old=("not set" if before is None else "blocked"),
                                          new="allowed"),
    )

    async def recheck() -> bool:
        try:
            check_post_access(channel, context.member, context.guild.me)
            check_member(owner, context.guild.me)
            return (workflow.support_reopen_state(context.guild, channel) == owner
                    and channel.overwrites_for(owner).send_messages == before)
        except ValueError:
            return False

    async def run() -> CommandOutcome:
        _, restored = await workflow.reopen_support_ticket(context.guild, channel,
                                                            context.member)
        if not restored:
            raise ValueError(ACTION_TICKET_REOPEN_UNAVAILABLE)
        return CommandOutcome("complete", "private", text=ACTION_TICKET_REOPEN_LABEL)

    context.state.command_proposals.append(PreparedAction(
        "reopen_support_ticket", {"channel_id": channel.id, "owner_id": owner.id},
        ChangePreview(lines, recheck, summary=ACTION_TICKET_REOPEN_LABEL),
        run, action_class=ActionClass.CHANGE,
    ))
    return {"status": "confirmation_required"}
