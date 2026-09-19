"""Status-only, permission-filtered active recruitment trial tools."""

from __future__ import annotations

from typing import Any, Mapping
from uuid import uuid4

import discord

from elbow_helper.configuration.channels import RECRUITMENT_TICKET_CATEGORY
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import accessible_message_channel, require_evidence_access
from ..reports.base import ArtifactCapacityError, retain_report
from ..models import AgentRequestContext, RegisteredAgentTool
from ..reports.recruitment import RecruitmentTrialReport


_TIMING_STATUSES = {"due", "in_progress"}


def recruitment_tools() -> tuple[RegisteredAgentTool, ...]:
    definitions = (
        (
            "read_active_recruitment_trials",
            "Read and retain status-only active recruitment trials from ticket channels the requester and bot can access. Returns applicant and configured trial dates; ticket content, reminders, recruiter notes and outcomes are excluded. This does not change recruitment.",
            {},
            (),
            read_active_recruitment_trials,
        ),
        (
            "read_active_recruitment_trial_report",
            "Read another filtered page from a retained status-only active recruitment trial report without rereading private state.",
            {
                "report_id": {"type": "string", "maxLength": 32},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                "ticket_channel_id": {"type": "integer", "minimum": 1},
                "applicant_member_id": {"type": "integer", "minimum": 1},
                "timing_status": {
                    "type": "string",
                    "enum": sorted(_TIMING_STATUSES),
                },
            },
            ("report_id",),
            read_active_recruitment_trial_report,
        ),
    )
    return tuple(
        RegisteredAgentTool(
            AgentToolDefinition(
                name=name,
                description=description,
                parameters={
                    "type": "object",
                    "properties": properties,
                    "required": list(required),
                    "additionalProperties": False,
                },
            ),
            handler,
        )
        for name, description, properties, required, handler in definitions
    )


async def read_active_recruitment_trials(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    del arguments
    await require_evidence_access(context)
    if context.recruitment_queries is None:
        return {"error": "Recruitment trial status is not available."}
    try:
        registrations = context.recruitment_queries.active_trial_registrations()
    except ValueError:
        return {"error": "Stored recruitment trials could not be listed."}
    accessible_ids = []
    for registration in registrations:
        channel = await accessible_message_channel(
            context, registration.ticket_channel_id,
        )
        if (
            channel is not None
            and getattr(channel, "category_id", None) == RECRUITMENT_TICKET_CATEGORY
            and getattr(channel, "type", None) == discord.ChannelType.text
        ):
            accessible_ids.append(registration.ticket_channel_id)
    try:
        snapshot = context.recruitment_queries.active_trial_snapshot(
            ticket_channel_ids=tuple(accessible_ids),
        )
        if not {row.ticket_channel_id for row in snapshot.trials}.issubset(
            accessible_ids
        ):
            raise ValueError("Recruitment snapshot includes an unauthorized channel")
        report = RecruitmentTrialReport(uuid4().hex, context.guild.id, snapshot)
    except ValueError:
        return {"error": "Stored recruitment trial status could not be read."}
    context.state.source_channels.update(accessible_ids)
    await require_evidence_access(context)
    if not report.snapshot.trials:
        return {**report.manifest(), "report_id": None}
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {
            "error": (
                "The complete recruitment trial report is too large to retain "
                "in this conversation."
            )
        }
    return report.page()


async def read_active_recruitment_trial_report(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if not isinstance(report, RecruitmentTrialReport) or report.guild_id != context.guild.id:
        return {
            "error": (
                "That recruitment trial report is not available in this "
                "conversation. Read the active trials again."
            )
        }
    for key in ("ticket_channel_id", "applicant_member_id"):
        value = arguments.get(key)
        if value is not None and (type(value) is not int or value <= 0):
            return {"error": f"A valid {key.replace('_', ' ')} is required."}
    timing_status = arguments.get("timing_status")
    if timing_status is not None and timing_status not in _TIMING_STATUSES:
        return {
            "error": "Unknown recruitment trial timing status.",
            "known_timing_statuses": sorted(_TIMING_STATUSES),
        }
    result = report.page(
        offset=arguments.get("offset", 0),
        limit=arguments.get("limit", 25),
        ticket_channel_id=arguments.get("ticket_channel_id"),
        applicant_member_id=arguments.get("applicant_member_id"),
        timing_status=timing_status,
    )
    await require_evidence_access(context)
    return result


__all__ = ["recruitment_tools"]
