"""Permission-filtered metadata reads for current support tickets."""

from __future__ import annotations

from typing import Any, Mapping
from uuid import uuid4

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import accessible_message_channel, require_evidence_access
from ..reports.base import ArtifactCapacityError, retain_report
from ..models import AgentRequestContext, RegisteredAgentTool
from ..reports.support import SupportTicketReport


_ACTIVITY_STATUSES = {"no_message_id", "channel_last_message"}


def support_tools() -> tuple[RegisteredAgentTool, ...]:
    definitions = (
        (
            "read_accessible_support_tickets",
            "Read and retain metadata for current support-ticket channels the requester and bot can access. This includes owner/send and channel activity timestamps, but does not read messages or decide whether a question was answered.",
            {},
            (),
            read_accessible_support_tickets,
        ),
        (
            "read_support_ticket_report",
            "Read another filtered page from a retained support-ticket metadata report without reading ticket messages.",
            {
                "report_id": {"type": "string", "maxLength": 32},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                "channel_id": {"type": "integer", "minimum": 1},
                "owner_member_id": {"type": "integer", "minimum": 1},
                "owner_can_send": {"type": "boolean"},
                "activity_status": {
                    "type": "string",
                    "enum": sorted(_ACTIVITY_STATUSES),
                },
            },
            ("report_id",),
            read_support_ticket_report,
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


async def read_accessible_support_tickets(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    del arguments
    await require_evidence_access(context)
    if context.support_queries is None:
        return {"error": "Support ticket metadata is not available."}
    try:
        registrations = context.support_queries.ticket_registrations(context.guild)
    except ValueError:
        return {"error": "Support ticket channels could not be listed."}
    accessible = []
    for registration in registrations:
        channel = await accessible_message_channel(context, registration.channel_id)
        if channel is not None:
            accessible.append(channel)
    try:
        snapshot = context.support_queries.metadata_snapshot(tuple(accessible))
        if {row.channel_id for row in snapshot.tickets} != {
            channel.id for channel in accessible
        }:
            raise ValueError("Support ticket snapshot does not match authorized channels")
        report = SupportTicketReport(
            uuid4().hex,
            context.guild.id,
            len(registrations),
            len(registrations) - len(accessible),
            snapshot,
        )
    except ValueError:
        return {"error": "Support ticket metadata could not be read."}
    context.state.source_channels.update(row.channel_id for row in snapshot.tickets)
    await require_evidence_access(context)
    if not report.snapshot.tickets:
        return {**report.manifest(), "report_id": None}
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {
            "error": (
                "The complete support ticket metadata report is too large to retain "
                "in this conversation."
            )
        }
    return report.page()


async def read_support_ticket_report(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if not isinstance(report, SupportTicketReport) or report.guild_id != context.guild.id:
        return {
            "error": (
                "That support ticket metadata report is not available in this "
                "conversation. Read the ticket metadata again."
            )
        }
    for key in ("channel_id", "owner_member_id"):
        value = arguments.get(key)
        if value is not None and (type(value) is not int or value <= 0):
            return {"error": f"A valid {key.replace('_', ' ')} is required."}
    owner_can_send = arguments.get("owner_can_send")
    if owner_can_send is not None and type(owner_can_send) is not bool:
        return {"error": "The owner send-permission filter must be true or false."}
    activity_status = arguments.get("activity_status")
    if activity_status is not None and activity_status not in _ACTIVITY_STATUSES:
        return {
            "error": "Unknown support ticket activity status.",
            "known_activity_statuses": sorted(_ACTIVITY_STATUSES),
        }
    result = report.page(
        offset=arguments.get("offset", 0),
        limit=arguments.get("limit", 25),
        channel_id=arguments.get("channel_id"),
        owner_member_id=arguments.get("owner_member_id"),
        owner_can_send=owner_can_send,
        activity_status=activity_status,
    )
    await require_evidence_access(context)
    return result


__all__ = ["support_tools"]
