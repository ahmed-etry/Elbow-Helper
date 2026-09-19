"""Permission-filtered adapters for member-lifecycle observations."""

from __future__ import annotations

from typing import Any, Mapping
from uuid import uuid4

from elbow_helper.configuration.channels import OVERSEEING_TERRACE
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import accessible_message_channel, require_evidence_access
from ..reports.base import ArtifactCapacityError, retain_report
from ..reports.member_lifecycle import MemberLifecycleReport
from ..models import AgentRequestContext, RegisteredAgentTool


def member_lifecycle_tools() -> tuple[RegisteredAgentTool, ...]:
    activity = {
        "type": "string", "enum": ["all", "observed", "not_observed"],
    }
    definitions = (
        (
            "read_member_lifecycle",
            "Read and retain the lifecycle feature's tracked current joins, recruitment-source counters, overdue-applicant scan results, and last message observations from channels the requester and bot can still access. Coverage is explicit: this is not complete server history, ticket content, or an inactivity verdict, and it changes nothing.",
            {
                "platform": {"type": "string", "minLength": 1, "maxLength": 100},
                "activity": activity,
                "overdue_only": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            },
            (), read_member_lifecycle,
        ),
        (
            "read_member_lifecycle_report",
            "Read another filtered page from one retained member-lifecycle snapshot. Current access to the overseeing source and every included activity channel is rechecked; mutable lifecycle state is not reread.",
            {
                "report_id": {"type": "string", "minLength": 1, "maxLength": 32},
                "platform": {"type": "string", "minLength": 1, "maxLength": 100},
                "activity": activity,
                "overdue_only": {"type": "boolean"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            },
            ("report_id",), read_member_lifecycle_report,
        ),
    )
    return tuple(RegisteredAgentTool(
        AgentToolDefinition(
            name=name, description=description,
            parameters={
                "type": "object", "properties": properties,
                "required": list(required), "additionalProperties": False,
            },
        ), handler,
    ) for name, description, properties, required, handler in definitions)


async def read_member_lifecycle(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.member_lifecycle_queries is None:
        return {"error": "Member lifecycle observations are not available."}
    if await accessible_message_channel(context, OVERSEEING_TERRACE) is None:
        return {"error": "The member lifecycle source is not accessible."}
    current_members = {
        member.id: member.display_name for member in context.guild.members
    }
    try:
        registrations = context.member_lifecycle_queries.activity_registrations(
            current_member_ids=set(current_members),
        )
    except ValueError:
        return {"error": "Stored member lifecycle observations could not be listed."}
    activity_channels = {}
    for registration in registrations:
        if await accessible_message_channel(context, registration.channel_id) is not None:
            activity_channels[registration.member_id] = registration.channel_id
    try:
        snapshot = context.member_lifecycle_queries.snapshot(
            current_members=current_members,
            activity_channels=activity_channels,
        )
        report = MemberLifecycleReport(
            uuid4().hex, context.guild.id, OVERSEEING_TERRACE, snapshot,
        )
    except (RuntimeError, TypeError, ValueError):
        return {"error": "Stored member lifecycle observations could not be read."}
    context.state.source_channels.update(report.source_channels)
    await require_evidence_access(context)
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {
            "error": (
                "The complete member lifecycle snapshot is too large to retain "
                "in this conversation."
            ),
        }
    try:
        return _page(report, arguments)
    except ValueError as error:
        context.state.reports.pop(report.report_id, None)
        return {"error": str(error)}


async def read_member_lifecycle_report(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if (
        not isinstance(report, MemberLifecycleReport)
        or report.guild_id != context.guild.id
    ):
        return {
            "error": (
                "That member lifecycle snapshot is not available in this "
                "conversation."
            ),
        }
    try:
        result = _page(report, arguments)
    except ValueError as error:
        return {"error": str(error)}
    await require_evidence_access(context)
    return result


def _page(
    report: MemberLifecycleReport, arguments: Mapping[str, Any],
) -> dict[str, Any]:
    return report.page(
        platform=arguments.get("platform"),
        activity=arguments.get("activity", "all"),
        overdue_only=arguments.get("overdue_only", False),
        offset=arguments.get("offset", 0),
        limit=arguments.get("limit", 25),
    )


__all__ = [
    "member_lifecycle_tools", "read_member_lifecycle",
    "read_member_lifecycle_report",
]
