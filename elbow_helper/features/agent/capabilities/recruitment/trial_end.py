"""Confirmed end of a recruitment trial."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ...engine.capability_contract import CapabilityContract
from ...access import require_evidence_access
from ...actions.contracts import ActionRefused, ActionClass, ChangePreview, PreparedAction
from ...actions.outcomes import ActionOutcome
from ...models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ...wording import (
    ACTION_TRIAL_END_LINE,
    ACTION_TRIAL_END_RENAME,
    ACTION_TRIAL_END_TRACKING,
    ACTION_TRIAL_END_REMINDER,
    ACTION_TRIAL_END_FOLLOWUP,
    ACTION_TRIAL_END_LABEL,
    ACTION_TRIAL_END_DONE,
)
from ...discord_actions.safety import check_member, check_post_access, resolve_channel


def trial_end_tools() -> tuple[RegisteredAgentTool, ...]:
    return (RegisteredAgentTool(AgentToolDefinition(
        name="end_recruitment_trial",
        description="End a recruit's active or expired trial and post the follow-up after confirmation.",
        parameters={"type": "object", "properties": {
            "ticket_channel_id": {"type": "integer", "minimum": 1},
            "applicant_id": {"type": "integer", "minimum": 1},
        }, "required": ["ticket_channel_id"], "additionalProperties": False},
    ), prepare_trial_end, AgentCapabilityEffect.COMMAND,
        ActionClass.IRREVERSIBLE,
        contract=CapabilityContract(entity_fields=(
                ("ticket_channel_id", "recruitment_ticket_channel"),
                ("applicant_id", "discord_member"),
            ), source_scope="request_context", channel_fields=("ticket_channel_id",)),
            ),)


async def prepare_trial_end(context: AgentRequestContext,
                            values: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    workflow = context.bot.get_cog("Recruitment")
    if workflow is None or not workflow.can_end_trial(context.member):
        raise ActionRefused("That trial isn't available.")
    snapshot = await workflow.trial_end_snapshot(values["ticket_channel_id"])
    trial, reminder = snapshot["trial"], snapshot["reminder"]
    if not snapshot["channel_name"] or not trial and not reminder:
        raise ActionRefused("That trial isn't available.")
    if reminder and reminder.get("resolved_at"):
        return {"status": "no_change"}
    applicant_id = values.get("applicant_id") or (trial or {}).get("applicant_id") or (
        reminder or {}).get("applicant_id")
    if not applicant_id:
        return {"status": "needs_input", "issue": "Which applicant's trial should end?"}
    if trial and trial.get("applicant_id") and int(trial["applicant_id"]) != applicant_id:
        raise ActionRefused("That trial isn't available.")
    ticket = await resolve_channel(context, values["ticket_channel_id"])
    check_post_access(ticket, context.member, context.guild.me)
    member = context.guild.get_member(applicant_id)
    if member is not None:
        check_member(member, context.guild.me, requester=context.member, guild=context.guild)
    channels = [ticket]
    for entry in (trial, reminder):
        if not entry:
            continue
        channel_id = entry.get("tracking_channel_id") or entry.get("channel_id")
        if channel_id:
            channel = await resolve_channel(context, int(channel_id))
            check_post_access(channel, context.member, context.guild.me)
            channels.append(channel)
    lines = [ACTION_TRIAL_END_LINE.format(member=f"<@{applicant_id}>",
                                          channel=ticket.mention)]
    if snapshot["new_name"] and snapshot["new_name"] != snapshot["channel_name"]:
        lines.append(ACTION_TRIAL_END_RENAME.format(
            old=snapshot["channel_name"], new=snapshot["new_name"]))
    if trial and trial.get("tracking_msg_id") and trial.get("tracking_channel_id"):
        lines.append(ACTION_TRIAL_END_TRACKING.format(
            channel=f"<#{trial['tracking_channel_id']}>"))
    lines.append(ACTION_TRIAL_END_FOLLOWUP)
    details = (workflow.trial_end_followup_text(applicant_id),)
    if reminder and reminder.get("message_id") and reminder.get("channel_id"):
        lines.append(ACTION_TRIAL_END_REMINDER.format(
            channel=f"<#{reminder['channel_id']}>"))

    async def recheck() -> bool:
        try:
            for channel in channels:
                check_post_access(channel, context.member, context.guild.me)
            if member is not None:
                check_member(member, context.guild.me,
                    requester=context.member, guild=context.guild)
            return await workflow.trial_end_snapshot(ticket.id) == snapshot
        except ValueError:
            return False

    async def run() -> ActionOutcome:
        result = await workflow.complete_trial_end(
            ticket.id, applicant_id, context.member,
            allow_missing=trial is None, resolve_reminder=reminder is not None)
        if not result.ended:
            raise ActionRefused(result.error or "That trial isn't available.")
        return ActionOutcome("complete", "private", text="\n".join(
            (ACTION_TRIAL_END_DONE, *result.notices)))

    context.state.proposed_changes.append(PreparedAction(
        "end_recruitment_trial", {"ticket_channel_id": ticket.id,
                                  "applicant_id": applicant_id},
        ChangePreview(tuple(lines), recheck, summary=ACTION_TRIAL_END_LABEL, details=details),
        run, action_class=ActionClass.IRREVERSIBLE,
    ))
    return {"status": "confirmation_required"}
