"""Confirmed refresh of a CWL prep board."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.features.cwl.config import DASHBOARD_THREADS
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ...engine.capability_contract import CapabilityContract
from ...access import require_evidence_access
from ...actions.contracts import ActionClass, ChangePreview, PreparedAction
from ...actions.outcomes import CommandOutcome
from ...models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ...wording import (
    ACTION_CWL_PREP_REFRESH_LINE,
    ACTION_CWL_PREP_REFRESH_LABEL,
)
from ...discord_actions.safety import check_post_access, resolve_channel


def cwl_prep_refresh_tools() -> tuple[RegisteredAgentTool, ...]:
    return (RegisteredAgentTool(AgentToolDefinition(
        name="refresh_cwl_prep_board",
        description="Refresh one clan's CWL prep board after confirmation.",
        parameters={"type": "object", "properties": {
            "clan_code": {"type": "string", "enum": list(DASHBOARD_THREADS)},
        }, "required": ["clan_code"], "additionalProperties": False},
    ), prepare_cwl_prep_refresh, AgentCapabilityEffect.COMMAND,
        ActionClass.CHANGE, True,
        contract=CapabilityContract(
            entity_fields=(("clan_code", "clan"),),
            time_fields=(),
            source_scope="request_context",
        ),
            ),)


async def prepare_cwl_prep_refresh(context: AgentRequestContext,
                                   values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("CwlManagement")
    if workflow is None:
        raise ValueError("That CWL prep board couldn't be refreshed.")
    channel = await resolve_channel(context, DASHBOARD_THREADS[values["clan_code"]])
    check_post_access(channel, context.member, context.guild.me)
    lines = (ACTION_CWL_PREP_REFRESH_LINE.format(
        clan=values["clan_code"], channel=channel.mention),)

    async def recheck() -> bool:
        try:
            check_post_access(channel, context.member, context.guild.me)
            return True
        except ValueError:
            return False

    async def run() -> CommandOutcome:
        status, _ = await workflow.refresh_prep_dashboard(values["clan_code"])
        if status != "complete":
            raise ValueError("That CWL prep board couldn't be refreshed.")
        return CommandOutcome("complete", "private", text=ACTION_CWL_PREP_REFRESH_LABEL)

    context.state.command_proposals.append(PreparedAction(
        "refresh_cwl_prep_board", dict(values),
        ChangePreview(lines, recheck, summary=ACTION_CWL_PREP_REFRESH_LABEL),
        run, action_class=ActionClass.CHANGE,
    ))
    return {"status": "confirmation_required"}
