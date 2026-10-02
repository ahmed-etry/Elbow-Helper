"""Confirmed Clan Castle status changes on CWL thread boards."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.features.cwl.config import CWL_CLAN_TAGS
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ...engine.capability_contract import CapabilityContract
from ...access import require_evidence_access
from ...actions.contracts import ActionClass, ChangePreview, PreparedAction
from ...actions.outcomes import CommandOutcome
from ...models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ...wording import (
    ACTION_CC_STATUS_LINE,
    ACTION_CC_STATUS_FIELD,
    ACTION_CC_STATUS_POST,
    ACTION_CC_STATUS_LABEL,
)
from ...discord_actions.safety import check_post_access, resolve_channel


def cwl_cc_status_tools() -> tuple[RegisteredAgentTool, ...]:
    return (RegisteredAgentTool(AgentToolDefinition(
        name="set_cwl_cc_status",
        description="Mark a clan's active CWL preparation Clan Castles filled, partial or empty after confirmation.",
        parameters={"type": "object", "properties": {
            "clan_code": {"type": "string", "enum": list(CWL_CLAN_TAGS)},
            "status": {"type": "string", "enum": ["filled", "partial", "empty"]},
        }, "required": ["clan_code", "status"], "additionalProperties": False},
    ), prepare_cc_status, AgentCapabilityEffect.COMMAND,
        ActionClass.CHANGE, True,
        contract=CapabilityContract(
            entity_fields=(("clan_code", "clan"),),
            time_fields=(),
            source_scope="request_context",
            filter_fields=("status",),
        ),
            ),)


async def prepare_cc_status(context: AgentRequestContext,
                            values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("CwlManagement")
    if workflow is None or not workflow.can_change_cc_status(context.member):
        raise ValueError("That CWL Clan Castle status isn't available.")
    snapshot = await workflow.cc_status_snapshot(values["clan_code"])
    if snapshot is None or not snapshot["sticky_message_id"]:
        raise ValueError("That CWL Clan Castle status isn't available.")
    if snapshot["status"] == values["status"]:
        return {"status": "no_change"}
    thread = await resolve_channel(context, snapshot["thread_id"])
    check_post_access(thread, context.member, context.guild.me)
    lines = (ACTION_CC_STATUS_LINE.format(clan=values["clan_code"],
                                          round=snapshot["round"],
                                          season=snapshot["season"]),
             ACTION_CC_STATUS_FIELD.format(old=snapshot["status"] or "empty",
                                           new=values["status"]),
             ACTION_CC_STATUS_POST.format(channel=thread.mention))

    async def recheck() -> bool:
        try:
            check_post_access(thread, context.member, context.guild.me)
            return await workflow.cc_status_snapshot(values["clan_code"]) == snapshot
        except ValueError:
            return False

    async def run() -> CommandOutcome:
        ok, message = await workflow.set_cwl_cc_status(
            values["clan_code"], values["status"], context.member,
            thread.id, snapshot["sticky_message_id"])
        if not ok:
            raise ValueError(message)
        return CommandOutcome("complete", "private", text=message,
                              after={"status": values["status"],
                                     "war_tag": snapshot["war_tag"]})

    context.state.command_proposals.append(PreparedAction(
        "set_cwl_cc_status", {"clan_code": values["clan_code"]},
        ChangePreview(lines, recheck, summary=ACTION_CC_STATUS_LABEL,
                      before={"status": snapshot["status"] or "empty",
                              "war_tag": snapshot["war_tag"]}), run,
    ))
    return {"status": "confirmation_required"}


async def prepare_cc_status_undo(context: AgentRequestContext,
                                 log: Mapping[str, Any]) -> PreparedAction:
    workflow = context.bot.get_cog("CwlManagement")
    if workflow is None:
        raise ValueError("That CWL Clan Castle status isn't available.")
    clan = log["targets"]["clan_code"]
    snapshot = await workflow.cc_status_snapshot(clan)
    if snapshot is None or snapshot["war_tag"] != log["after"]["war_tag"] or (
            snapshot["status"] != log["after"]["status"]):
        raise ValueError("That CWL Clan Castle status isn't available.")
    thread = await resolve_channel(context, snapshot["thread_id"])
    check_post_access(thread, context.member, context.guild.me)
    prior = log["before"]["status"]
    lines = (ACTION_CC_STATUS_LINE.format(clan=clan, round=snapshot["round"],
                                          season=snapshot["season"]),
             ACTION_CC_STATUS_FIELD.format(old=snapshot["status"], new=prior),
             ACTION_CC_STATUS_POST.format(channel=thread.mention))

    async def recheck() -> bool:
        live = await workflow.cc_status_snapshot(clan)
        return live == snapshot

    async def run() -> CommandOutcome:
        ok, message = await workflow.set_cwl_cc_status(
            clan, prior, context.member, thread.id, snapshot["sticky_message_id"])
        if not ok:
            raise ValueError(message)
        return CommandOutcome("complete", "private", text=message,
                              after={"status": prior, "war_tag": snapshot["war_tag"]})

    return PreparedAction(
        "undo_cwl_cc_status", {"clan_code": clan},
        ChangePreview(lines, recheck, summary=ACTION_CC_STATUS_LABEL,
                      before=log["after"]), run,
    )


UNDO_HANDLERS = {
    'set_cwl_cc_status': prepare_cc_status_undo,
    'undo_cwl_cc_status': prepare_cc_status_undo,
}
