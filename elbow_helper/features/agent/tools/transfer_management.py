"""Confirmed action for a transfer queue's clear control."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..actions.contracts import ActionClass, ChangePreview, PreparedAction
from ..commands.outcomes import CommandOutcome
from ..models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ..wording import (
    ACTION_TRANSFER_CLEAR_LINE, ACTION_TRANSFER_CLEAR_MEMBER,
    ACTION_TRANSFER_CLEAR_PING, ACTION_TRANSFER_CLEAR_BOARDS,
    ACTION_TRANSFER_CLEAR_LABEL, ACTION_TRANSFER_UNAVAILABLE,
)


def transfer_management_tools() -> tuple[RegisteredAgentTool, ...]:
    return (RegisteredAgentTool(AgentToolDefinition(
        name="clear_transfer_queue",
        description="Clear all pending requests from a clan transfer queue after confirmation.",
        parameters={"type": "object", "properties": {
            "clan_code": {"type": "string"},
        }, "required": ["clan_code"], "additionalProperties": False},
    ), prepare_clear_transfer_queue, AgentCapabilityEffect.COMMAND,
        ActionClass.IRREVERSIBLE, True),)


async def prepare_clear_transfer_queue(context: AgentRequestContext,
                                       values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("ClanTransfers")
    if workflow is None:
        raise ValueError(ACTION_TRANSFER_UNAVAILABLE)
    clan_code = values["clan_code"]
    state = workflow.transfer_queue_clear_state(clan_code)
    if state is None:
        raise ValueError(ACTION_TRANSFER_UNAVAILABLE)
    lines = [ACTION_TRANSFER_CLEAR_LINE.format(clan=clan_code)]
    lines.extend(ACTION_TRANSFER_CLEAR_MEMBER.format(member=f"<@{member_id}>")
                 for member_id in state["member_ids"])
    if state["ping_message_id"]:
        lines.append(ACTION_TRANSFER_CLEAR_PING.format(
            message_id=state["ping_message_id"], thread=f"<#{state['thread_id']}>",
        ))
    lines.append(ACTION_TRANSFER_CLEAR_BOARDS.format(
        thread=f"<#{state['thread_id']}>", board=f"<#{state['board_channel_id']}>",
    ))

    async def recheck() -> bool:
        return workflow.transfer_queue_clear_state(clan_code) == state

    async def run() -> CommandOutcome:
        text = await workflow.clear_transfer_queue(clan_code)
        return CommandOutcome("complete", "private", text=text)

    context.state.command_proposals.append(PreparedAction(
        "clear_transfer_queue", {"clan_code": clan_code},
        ChangePreview(tuple(lines), recheck, summary=ACTION_TRANSFER_CLEAR_LABEL),
        run, action_class=ActionClass.IRREVERSIBLE,
    ))
    return {"status": "confirmation_required"}
