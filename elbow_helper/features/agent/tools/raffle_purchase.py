"""Confirmed raffle ticket purchases from the hub control."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..actions.contracts import ActionClass, ChangePreview, PreparedAction
from ..actions.outcomes import CommandOutcome
from ..models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ..wording import (
    ACTION_RAFFLE_BUY_LINE,
    ACTION_RAFFLE_BUY_BALANCE,
    ACTION_RAFFLE_HUB_UPDATE,
    ACTION_RAFFLE_BUY_LABEL,
)


def raffle_purchase_tools() -> tuple[RegisteredAgentTool, ...]:
    return (RegisteredAgentTool(AgentToolDefinition(
        name="buy_raffle_ticket",
        description="Buy this month's raffle ticket with the requester's own coins after confirmation.",
        parameters={"type": "object", "properties": {}, "required": [],
                    "additionalProperties": False},
    ), prepare_raffle_purchase, AgentCapabilityEffect.COMMAND,
        ActionClass.CHANGE, True),)


async def prepare_raffle_purchase(context: AgentRequestContext,
                                  values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("Achievements")
    if workflow is None:
        raise ValueError('Raffle ticket purchases are unavailable.')
    before = await workflow.raffle_purchase_state(context.member.id)
    if before["issue"]:
        return {"status": "needs_input", "issue": before["issue"],
                "prepared_count": 0}
    lines = (
        ACTION_RAFFLE_BUY_LINE.format(member=context.member.mention,
                                     cost=before["cost"]),
        ACTION_RAFFLE_BUY_BALANCE.format(
            old=before["balance"], new=before["balance"] - before["cost"]),
        ACTION_RAFFLE_HUB_UPDATE,
    )

    async def recheck() -> bool:
        return await workflow.raffle_purchase_state(context.member.id) == before

    async def run() -> CommandOutcome:
        ok, message = await workflow.buy_raffle_ticket(context.member.id)
        if not ok:
            raise ValueError(message)
        return CommandOutcome("complete", "private", text=message,
                              after={"balance": before["balance"] - before["cost"],
                                     "has_ticket": True})

    context.state.command_proposals.append(PreparedAction(
        "buy_raffle_ticket", {"member_id": context.member.id,
                              "month_key": before["month_key"]},
        ChangePreview(lines, recheck, summary=ACTION_RAFFLE_BUY_LABEL,
                      before={"balance": before["balance"],
                              "has_ticket": before["has_ticket"]}),
        run, action_class=ActionClass.CHANGE,
    ))
    return {"status": "confirmation_required"}
