"""Lead Plus-only, read-only leadership record tools."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping
from uuid import uuid4

from elbow_helper.features.records.domain.types import (
    CATEGORY_BY_KEY,
    INCIDENT_TYPE_BY_KEY,
)
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import (
    ACCESS_LEAD_PLUS,
    require_access_requirements,
    require_evidence_access,
)
from ..reports.base import ArtifactCapacityError, retain_report
from ..reports.leadership_record import LeadershipRecordReport
from ..models import AgentRequestContext, RegisteredAgentTool


def record_tools() -> tuple[RegisteredAgentTool, ...]:
    definitions = (
        (
            "read_active_leadership_records",
            "Read and retain active internal leadership records for all members or one Discord member. Requires the requester to currently have Lead Plus access. Returns stored incident details and attribution, excludes removed records and does not change records.",
            {
                "member_id": {"type": "integer", "minimum": 1},
            },
            (),
            read_active_leadership_records,
        ),
        (
            "read_leadership_record_report",
            "Read another filtered page from a retained active leadership-record report. Lead Plus access is rechecked and private state is not reread.",
            {
                "report_id": {"type": "string", "maxLength": 32},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                "member_id": {"type": "integer", "minimum": 1},
                "category_key": {
                    "type": "string", "enum": sorted(CATEGORY_BY_KEY),
                },
                "incident_type_key": {
                    "type": "string", "enum": sorted(INCIDENT_TYPE_BY_KEY),
                },
                "search": {
                    "type": "string", "minLength": 1, "maxLength": 200,
                },
            },
            ("report_id",),
            read_leadership_record_report,
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


async def read_active_leadership_records(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.record_queries is None:
        return {"error": "Leadership records are not available."}
    require_access_requirements(
        context.guild, context.member.id, {ACCESS_LEAD_PLUS},
    )
    member_id = arguments.get("member_id")
    if member_id is not None and (type(member_id) is not int or member_id <= 0):
        return {"error": "A valid member ID is required."}
    try:
        snapshot = await asyncio.to_thread(
            context.record_queries.active_snapshot,
            member_id=member_id,
        )
        report = LeadershipRecordReport(uuid4().hex, context.guild.id, snapshot)
    except ValueError:
        return {"error": "Active leadership records could not be read completely."}
    require_access_requirements(
        context.guild, context.member.id, {ACCESS_LEAD_PLUS},
    )
    context.state.required_access.add(ACCESS_LEAD_PLUS)
    await require_evidence_access(context)
    if not report.snapshot.records:
        return {**report.manifest(), "report_id": None}
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {
            "error": (
                "The complete leadership-record report is too large to retain "
                "in this conversation."
            )
        }
    return report.page()


async def read_leadership_record_report(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    require_access_requirements(
        context.guild, context.member.id, {ACCESS_LEAD_PLUS},
    )
    context.state.required_access.add(ACCESS_LEAD_PLUS)
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if not isinstance(report, LeadershipRecordReport) or report.guild_id != context.guild.id:
        return {
            "error": (
                "That leadership-record report is not available in this "
                "conversation. Read the active records again."
            )
        }
    member_id = arguments.get("member_id")
    if member_id is not None and (type(member_id) is not int or member_id <= 0):
        return {"error": "A valid member ID is required."}
    category_key = arguments.get("category_key")
    if category_key is not None and category_key not in CATEGORY_BY_KEY:
        return {
            "error": "Unknown leadership-record category.",
            "known_categories": sorted(CATEGORY_BY_KEY),
        }
    incident_type_key = arguments.get("incident_type_key")
    if (
        incident_type_key is not None
        and incident_type_key not in INCIDENT_TYPE_BY_KEY
    ):
        return {
            "error": "Unknown leadership-record incident type.",
            "known_incident_types": sorted(INCIDENT_TYPE_BY_KEY),
        }
    search = arguments.get("search")
    if search is not None and (
        not isinstance(search, str) or not search.strip() or len(search) > 200
    ):
        return {"error": "A search must contain between 1 and 200 characters."}
    result = report.page(
        offset=arguments.get("offset", 0),
        limit=arguments.get("limit", 25),
        member_id=member_id,
        category_key=category_key,
        incident_type_key=incident_type_key,
        search=search.strip() if search is not None else None,
    )
    await require_evidence_access(context)
    return result


__all__ = ["record_tools"]
