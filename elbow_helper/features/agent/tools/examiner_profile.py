"""Confirmed changes to the requester's examiner profile."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.configuration.channels import EXAMINATION_PANEL_THREAD
from elbow_helper.features.examination.config import TIMEZONE_SELECT_OPTIONS
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..actions.contracts import ActionClass, ChangePreview, PreparedAction
from ..commands.outcomes import CommandOutcome
from ..models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ..wording import (
    ACTION_EXAMINER_PROFILE_LEAVE_LABEL,
    ACTION_EXAMINER_PROFILE_LINE, ACTION_EXAMINER_PROFILE_FIELD,
    ACTION_EXAMINER_PROFILE_LABEL, ACTION_EXAMINER_PROFILE_LEAVE,
    ACTION_EXAMINER_PROFILE_UNAVAILABLE,
)
from .discord_safety import check_post_access, resolve_channel


PROFILE_FIELDS = ("th_levels", "status", "timezone", "availability")


def examiner_profile_tools() -> tuple[RegisteredAgentTool, ...]:
    schema = {"type": "object", "properties": {
        "th_levels": {"type": "array", "items": {"type": "integer", "minimum": 11, "maximum": 18},
                      "minItems": 1, "maxItems": 8, "uniqueItems": True},
        "status": {"type": "string", "enum": ["Active", "Away"]},
        "timezone": {"type": "string", "enum": [option.value for option in TIMEZONE_SELECT_OPTIONS]},
        "availability": {"type": "string", "maxLength": 120},
    }, "additionalProperties": False}
    return (
        RegisteredAgentTool(AgentToolDefinition(
            name="set_examiner_profile",
            description="Set your examiner Town Hall coverage, status, timezone or availability after confirmation.",
            parameters=schema,
        ), prepare_examiner_profile, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE, True),
        RegisteredAgentTool(AgentToolDefinition(
            name="leave_examiner_roster",
            description="Remove yourself from the examiner roster after confirmation.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        ), prepare_examiner_leave, AgentCapabilityEffect.COMMAND, ActionClass.IRREVERSIBLE, True),
    )


async def _workflow(context: AgentRequestContext):
    await require_evidence_access(context)
    workflow = context.bot.get_cog("Examination")
    if workflow is None or not workflow.can_edit_examiner_profile(context.member):
        raise ValueError(ACTION_EXAMINER_PROFILE_UNAVAILABLE)
    channel = await resolve_channel(context, EXAMINATION_PANEL_THREAD)
    check_post_access(channel, context.member, context.guild.me)
    return workflow, channel


async def prepare_examiner_profile(context: AgentRequestContext,
                                   values: Mapping[str, Any]) -> Mapping[str, Any]:
    workflow, channel = await _workflow(context)
    before = workflow.examiner_profile_snapshot(context.member)
    existed = workflow.has_examiner_profile(context.member)
    try:
        after = workflow.prepare_examiner_profile_change(context.member, dict(values))
    except ValueError as exc:
        return {"status": "needs_input", "issue": str(exc), "prepared_count": 0}
    changed = [key for key in PROFILE_FIELDS if before.get(key) != after.get(key)]
    if not changed:
        return {"status": "no_change"}
    lines = [ACTION_EXAMINER_PROFILE_LINE.format(member=context.member.mention,
                                                 channel=channel.mention)]
    lines.extend(ACTION_EXAMINER_PROFILE_FIELD.format(
        field=key.replace("_", " ").title(), old=before.get(key) or "Not set",
        new=after.get(key) or "Not set") for key in changed)

    async def recheck() -> bool:
        try:
            check_post_access(channel, context.member, context.guild.me)
            return (workflow.has_examiner_profile(context.member) == existed
                    and workflow.examiner_profile_snapshot(context.member) == before)
        except ValueError:
            return False

    async def run() -> CommandOutcome:
        profile = await workflow.change_examiner_profile(context.member, dict(values))
        return CommandOutcome("complete", "private", text=ACTION_EXAMINER_PROFILE_LABEL,
                              after={"profile": {key: profile[key] for key in PROFILE_FIELDS},
                                     "existed": True})

    context.state.command_proposals.append(PreparedAction(
        "set_examiner_profile", {"member_id": context.member.id},
        ChangePreview(tuple(lines), recheck, summary=ACTION_EXAMINER_PROFILE_LABEL,
                      before={"profile": {key: before[key] for key in PROFILE_FIELDS},
                              "existed": existed}), run,
    ))
    return {"status": "confirmation_required"}


async def prepare_examiner_leave(context: AgentRequestContext,
                                 values: Mapping[str, Any]) -> Mapping[str, Any]:
    workflow, channel = await _workflow(context)
    if not workflow.has_examiner_profile(context.member):
        return {"status": "no_change"}
    before = workflow.examiner_profile_snapshot(context.member)
    lines = (ACTION_EXAMINER_PROFILE_LEAVE.format(member=context.member.mention,
                                                 channel=channel.mention),)

    async def recheck() -> bool:
        try:
            check_post_access(channel, context.member, context.guild.me)
            return workflow.examiner_profile_snapshot(context.member) == before
        except ValueError:
            return False

    async def run() -> CommandOutcome:
        if not await workflow.leave_examiner_roster(context.member):
            raise ValueError(ACTION_EXAMINER_PROFILE_UNAVAILABLE)
        return CommandOutcome("complete", "private", text=ACTION_EXAMINER_PROFILE_LEAVE.format(
            member=context.member.mention, channel=channel.mention))

    context.state.command_proposals.append(PreparedAction(
        "leave_examiner_roster", {"member_id": context.member.id},
        ChangePreview(lines, recheck, summary=ACTION_EXAMINER_PROFILE_LEAVE_LABEL),
        run, action_class=ActionClass.IRREVERSIBLE,
    ))
    return {"status": "confirmation_required"}


async def prepare_examiner_profile_undo(context: AgentRequestContext,
                                        log: Mapping[str, Any]) -> PreparedAction:
    workflow, channel = await _workflow(context)
    prior, expected = log["before"], log["after"]
    current = workflow.examiner_profile_snapshot(context.member)
    if not workflow.has_examiner_profile(context.member) or any(
            current[key] != expected["profile"][key] for key in PROFILE_FIELDS):
        raise ValueError(ACTION_EXAMINER_PROFILE_UNAVAILABLE)
    lines = [ACTION_EXAMINER_PROFILE_LINE.format(member=context.member.mention,
                                                 channel=channel.mention)]
    lines.extend(ACTION_EXAMINER_PROFILE_FIELD.format(
        field=key.replace("_", " ").title(), old=current[key] or "Not set",
        new=prior["profile"][key] or "Not set")
        for key in PROFILE_FIELDS if current[key] != prior["profile"][key])

    async def recheck() -> bool:
        live = workflow.examiner_profile_snapshot(context.member)
        return all(live[key] == expected["profile"][key] for key in PROFILE_FIELDS)

    async def run() -> CommandOutcome:
        if prior["existed"]:
            restored = await workflow.change_examiner_profile(context.member, prior["profile"])
            after = {"profile": {key: restored[key] for key in PROFILE_FIELDS}, "existed": True}
        else:
            await workflow.leave_examiner_roster(context.member)
            after = {"profile": prior["profile"], "existed": False}
        return CommandOutcome("complete", "private", text=ACTION_EXAMINER_PROFILE_LABEL,
                              after=after)

    return PreparedAction(
        "undo_examiner_profile", {"member_id": context.member.id},
        ChangePreview(tuple(lines), recheck, summary=ACTION_EXAMINER_PROFILE_LABEL,
                      before=expected), run,
    )
