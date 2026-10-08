"""Confirmed changes to the requester's examiner profile."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elbow_helper.configuration.channels import EXAMINATION_PANEL_THREAD
from elbow_helper.domain.timezones import format_timezone_display
from elbow_helper.features.examination.config import TIMEZONE_SELECT_OPTIONS
from elbow_helper.features.examination.panel import ExaminerProfileInputError
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ...engine.capability_contract import CapabilityContract
from ...access import require_evidence_access
from ...actions.contracts import ActionRefused, ActionClass, ChangePreview, PreparedAction
from ...actions.outcomes import ActionOutcome
from ...actions.values import display_value
from ...models import AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ...wording import (
    ACTION_EXAMINER_PROFILE_LINE,
    ACTION_FIELD_CHANGE,
    ACTION_EXAMINER_PROFILE_LABEL,
    ACTION_EXAMINER_PROFILE_LEAVE,
    ACTION_EXAMINER_PROFILE_LEAVE_LABEL,
    ACTION_EXAMINER_PROFILE_FIELDS,
)
from ...discord_actions.safety import check_post_access, check_view_access, resolve_channel


PROFILE_FIELDS = ("th_levels", "status", "timezone", "availability")


def _profile_value(field: str, value: Any) -> str:
    if field == "timezone" and value:
        return format_timezone_display(value)
    return display_value(value)


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
            name="read_my_examiner_profile",
            description="Read your examiner Town Hall coverage, status, timezone and availability.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        ), read_examiner_profile,
            contract=CapabilityContract(
                entity_fields=(),
                source_scope="channel_status",
                result_channel_fields=("panel_channel_id",),
            ),
        ),
        RegisteredAgentTool(AgentToolDefinition(
            name="read_examiner_roster",
            description="Read a page of examiner profiles shown by the examiner panel.",
            parameters={"type": "object", "properties": {
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            }, "additionalProperties": False},
        ), read_examiner_roster,
            contract=CapabilityContract(
                entity_fields=(),
                source_scope="channel_status",
                result_channel_fields=("panel_channel_id",),
                filter_fields=("offset", "limit"),
            ),
        ),
        RegisteredAgentTool(AgentToolDefinition(
            name="set_examiner_profile",
            description="Set your examiner Town Hall coverage, status, timezone or availability after confirmation.",
            parameters=schema,
        ), prepare_examiner_profile, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE,
            contract=CapabilityContract(
                entity_fields=(),
                source_scope="request_context",
                filter_fields=("th_levels", "status", "timezone", "availability"),
            ),
        ),
        RegisteredAgentTool(AgentToolDefinition(
            name="leave_examiner_roster",
            description="Remove yourself from the examiner roster after confirmation.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        ), prepare_examiner_leave, AgentCapabilityEffect.COMMAND, ActionClass.IRREVERSIBLE,
            contract=CapabilityContract(entity_fields=(), source_scope="request_context"),
        ),
    )


async def read_examiner_profile(context: AgentRequestContext,
                                values: Mapping[str, Any]) -> Mapping[str, Any]:
    workflow, channel = await _workflow(context, post=False)
    profile = workflow.examiner_profile_snapshot(context.member)
    await require_evidence_access(context)
    return {"panel_channel_id": channel.id,
            "registered": workflow.has_examiner_profile(context.member),
            "profile": {key: profile[key] for key in PROFILE_FIELDS},
            "profile_complete": profile["profile_complete"]}


async def read_examiner_roster(context: AgentRequestContext,
                               values: Mapping[str, Any]) -> Mapping[str, Any]:
    workflow, channel = await _workflow(context, post=False)
    rows = workflow.examiner_roster_snapshot()
    offset, limit = values.get("offset", 0), values.get("limit", 25)
    await require_evidence_access(context)
    return {"panel_channel_id": channel.id, "total": len(rows),
            "offset": offset, "profiles": rows[offset:offset + limit]}


async def _workflow(context: AgentRequestContext, *, post: bool = True):
    await require_evidence_access(context)
    workflow = context.bot.get_cog("Examination")
    if workflow is None or not workflow.can_edit_examiner_profile(context.member):
        raise ActionRefused("That examiner profile isn't available.")
    channel = await resolve_channel(context, EXAMINATION_PANEL_THREAD)
    if post:
        check_post_access(channel, context.member, context.guild.me)
    else:
        check_view_access(channel, context.member, context.guild.me)
    return workflow, channel


async def prepare_examiner_profile(context: AgentRequestContext,
                                   values: Mapping[str, Any]) -> Mapping[str, Any]:
    workflow, channel = await _workflow(context)
    before = workflow.examiner_profile_snapshot(context.member)
    existed = workflow.has_examiner_profile(context.member)
    try:
        after = workflow.prepare_examiner_profile_change(context.member, dict(values))
    except ValueError as exc:
        result = {"status": "needs_input", "issue": str(exc)}
        if isinstance(exc, ExaminerProfileInputError):
            result.update(field=exc.field, choices=exc.choices)
        return result
    changed = [key for key in PROFILE_FIELDS if before.get(key) != after.get(key)]
    if not changed:
        return {"status": "no_change"}
    lines = [ACTION_EXAMINER_PROFILE_LINE.format(member=context.member.mention,
                                                 channel=channel.mention)]
    details = tuple(ACTION_FIELD_CHANGE.format(
        field=ACTION_EXAMINER_PROFILE_FIELDS[key], old=_profile_value(key, before.get(key)),
        new=_profile_value(key, after.get(key))) for key in changed)

    async def recheck() -> bool:
        try:
            check_post_access(channel, context.member, context.guild.me)
            return (workflow.has_examiner_profile(context.member) == existed
                    and workflow.examiner_profile_snapshot(context.member) == before)
        except ValueError:
            return False

    async def run() -> ActionOutcome:
        profile = await workflow.change_examiner_profile(context.member, dict(values))
        return ActionOutcome("complete", "private", text=ACTION_EXAMINER_PROFILE_LABEL,
                              after={"profile": {key: profile[key] for key in PROFILE_FIELDS},
                                     "existed": True})

    context.state.proposed_changes.append(PreparedAction(
        "set_examiner_profile", {"member_id": context.member.id},
        ChangePreview(tuple(lines), recheck, summary=ACTION_EXAMINER_PROFILE_LABEL,
                      details=details, detail_sources=frozenset({channel.id}),
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

    async def run() -> ActionOutcome:
        if not await workflow.leave_examiner_roster(context.member):
            raise ActionRefused("That examiner profile isn't available.")
        return ActionOutcome("complete", "private", text=ACTION_EXAMINER_PROFILE_LEAVE.format(
            member=context.member.mention, channel=channel.mention))

    context.state.proposed_changes.append(PreparedAction(
        "leave_examiner_roster", {"member_id": context.member.id},
        ChangePreview(lines, recheck, summary=ACTION_EXAMINER_PROFILE_LEAVE_LABEL,
                      detail_sources=frozenset({channel.id})),
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
        raise ActionRefused("That examiner profile isn't available.")
    lines = [ACTION_EXAMINER_PROFILE_LINE.format(member=context.member.mention,
                                                 channel=channel.mention)]
    details = tuple(ACTION_FIELD_CHANGE.format(
        field=ACTION_EXAMINER_PROFILE_FIELDS[key], old=_profile_value(key, current[key]),
        new=_profile_value(key, prior["profile"][key]))
        for key in PROFILE_FIELDS if current[key] != prior["profile"][key])

    async def recheck() -> bool:
        live = workflow.examiner_profile_snapshot(context.member)
        return all(live[key] == expected["profile"][key] for key in PROFILE_FIELDS)

    async def run() -> ActionOutcome:
        if prior["existed"]:
            restored = await workflow.change_examiner_profile(context.member, prior["profile"])
            after = {"profile": {key: restored[key] for key in PROFILE_FIELDS}, "existed": True}
        else:
            await workflow.leave_examiner_roster(context.member)
            after = {"profile": prior["profile"], "existed": False}
        return ActionOutcome("complete", "private", text=ACTION_EXAMINER_PROFILE_LABEL,
                              after=after)

    return PreparedAction(
        "undo_examiner_profile", {"member_id": context.member.id},
        ChangePreview(tuple(lines), recheck, summary=ACTION_EXAMINER_PROFILE_LABEL,
                      details=details, detail_sources=frozenset({channel.id}),
                      before=expected), run,
    )


UNDO_HANDLERS = {
    'set_examiner_profile': prepare_examiner_profile_undo,
    'undo_examiner_profile': prepare_examiner_profile_undo,
}
