"""Status-only, permission-filtered examination case tools."""

from __future__ import annotations

from typing import Any, Mapping
from uuid import uuid4

import discord

from elbow_helper.configuration.channels import (
    EXAMINATION_ROOM,
    EXAMINATION_TICKET_CATEGORY,
)
from elbow_helper.features.examination.queries import CASE_TYPES, WORKFLOW_STATUSES
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import accessible_message_channel, require_evidence_access
from ..reports.base import ArtifactCapacityError, retain_report
from ..reports.examination import ExaminationCaseReport
from ..models import AgentRequestContext, RegisteredAgentTool


_RESPONSE_STATUSES = {"recorded", "not_recorded"}


def examination_tools() -> tuple[RegisteredAgentTool, ...]:
    definitions = (
        (
            "read_accessible_examination_cases",
            "Read and retain status-only promotion examination cases when the requester and bot can access the examination room and each included ticket. Returns workflow flags without message content, application answers, availability, examiner matches or outcomes. This does not change examinations.",
            {},
            (),
            read_accessible_examination_cases,
        ),
        (
            "read_examination_case_report",
            "Read another filtered page from a retained status-only examination case report without rereading private state.",
            {
                "report_id": {"type": "string", "maxLength": 32},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                "ticket_channel_id": {"type": "integer", "minimum": 1},
                "applicant_member_id": {"type": "integer", "minimum": 1},
                "case_type": {"type": "string", "enum": sorted(CASE_TYPES)},
                "workflow_status": {
                    "type": "string", "enum": sorted(WORKFLOW_STATUSES),
                },
                "response_status": {
                    "type": "string", "enum": sorted(_RESPONSE_STATUSES),
                },
            },
            ("report_id",),
            read_examination_case_report,
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


async def read_accessible_examination_cases(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    del arguments
    await require_evidence_access(context)
    if context.examination_queries is None:
        return {"error": "Examination case status is not available."}
    if await accessible_message_channel(context, EXAMINATION_ROOM) is None:
        return {
            "error": (
                "Examination case status is not accessible from this conversation."
            )
        }
    try:
        registrations = context.examination_queries.case_registrations()
    except ValueError:
        return {"error": "Stored examination cases could not be listed."}
    accessible_ids = []
    for registration in registrations:
        channel = await accessible_message_channel(
            context, registration.ticket_channel_id,
        )
        if (
            channel is not None
            and getattr(channel, "category_id", None) == EXAMINATION_TICKET_CATEGORY
            and getattr(channel, "type", None) == discord.ChannelType.text
        ):
            accessible_ids.append(registration.ticket_channel_id)
    try:
        snapshot = context.examination_queries.case_snapshot(
            ticket_channel_ids=tuple(accessible_ids),
        )
        if not {row.ticket_channel_id for row in snapshot.cases}.issubset(
            accessible_ids
        ):
            raise ValueError("Examination snapshot includes an unauthorized channel")
        report = ExaminationCaseReport(uuid4().hex, context.guild.id, snapshot)
    except ValueError:
        return {"error": "Stored examination case status could not be read."}
    context.state.source_channels.update((EXAMINATION_ROOM, *accessible_ids))
    await require_evidence_access(context)
    if not report.snapshot.cases:
        return {**report.manifest(), "report_id": None}
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {
            "error": (
                "The complete examination case report is too large to retain "
                "in this conversation."
            )
        }
    return report.page()


async def read_examination_case_report(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if not isinstance(report, ExaminationCaseReport) or report.guild_id != context.guild.id:
        return {
            "error": (
                "That examination case report is not available in this conversation. "
                "Read the examination cases again."
            )
        }
    for key in ("ticket_channel_id", "applicant_member_id"):
        value = arguments.get(key)
        if value is not None and (type(value) is not int or value <= 0):
            return {"error": f"A valid {key.replace('_', ' ')} is required."}
    case_type = arguments.get("case_type")
    if case_type is not None and case_type not in CASE_TYPES:
        return {
            "error": "Unknown examination case type.",
            "known_case_types": sorted(CASE_TYPES),
        }
    workflow_status = arguments.get("workflow_status")
    if workflow_status is not None and workflow_status not in WORKFLOW_STATUSES:
        return {
            "error": "Unknown examination workflow status.",
            "known_workflow_statuses": sorted(WORKFLOW_STATUSES),
        }
    response_status = arguments.get("response_status")
    if response_status is not None and response_status not in _RESPONSE_STATUSES:
        return {
            "error": "Unknown examination response status.",
            "known_response_statuses": sorted(_RESPONSE_STATUSES),
        }
    result = report.page(
        offset=arguments.get("offset", 0),
        limit=arguments.get("limit", 25),
        ticket_channel_id=arguments.get("ticket_channel_id"),
        applicant_member_id=arguments.get("applicant_member_id"),
        case_type=case_type,
        workflow_status=workflow_status,
        response_status=response_status,
    )
    await require_evidence_access(context)
    return result


__all__ = ["examination_tools"]
