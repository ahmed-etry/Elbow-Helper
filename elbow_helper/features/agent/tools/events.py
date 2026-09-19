"""Lead-only read adapters for configured event schedules and counters."""

from __future__ import annotations

from typing import Any, Mapping
from uuid import uuid4

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import (
    ACCESS_LEAD, require_access_requirements, require_evidence_access,
)
from ..reports.base import ArtifactCapacityError, retain_report
from ..reports.event import EventScheduleReport
from ..models import AgentRequestContext, RegisteredAgentTool


def event_tools() -> tuple[RegisteredAgentTool, ...]:
    phase = {
        "type": "string",
        "enum": ["all", "live", "upcoming", "ended", "expired", "disabled"],
    }
    event_type = {
        "type": "string",
        "enum": ["all", "counter", "recurring", "one-time"],
    }
    definitions = (
        (
            "read_event_schedule",
            "Read and retain the configured event trackers, exact current/next schedule windows and covered member counters. Requires current Lead access, reports missing counter roles explicitly, and does not refresh channels or change event settings.",
            {
                "phase": phase, "event_type": event_type,
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            },
            (), read_event_schedule,
        ),
        (
            "read_event_schedule_report",
            "Read another filtered page from one retained event-schedule snapshot. Current Lead and source access are rechecked; mutable event state is not reread.",
            {
                "report_id": {
                    "type": "string", "minLength": 1, "maxLength": 32,
                },
                "phase": phase, "event_type": event_type,
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            },
            ("report_id",), read_event_schedule_report,
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


async def read_event_schedule(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    require_access_requirements(
        context.guild, context.member.id, {ACCESS_LEAD},
    )
    await require_evidence_access(context)
    if context.event_queries is None:
        return {"error": "Event schedules are not available."}
    try:
        snapshot = context.event_queries.snapshot(context.guild)
        report = EventScheduleReport(
            uuid4().hex, context.guild.id, snapshot,
        )
    except (RuntimeError, TypeError, ValueError):
        return {"error": "Event schedules could not be read completely."}
    require_access_requirements(
        context.guild, context.member.id, {ACCESS_LEAD},
    )
    context.state.required_access.add(ACCESS_LEAD)
    await require_evidence_access(context)
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {
            "error": (
                "The complete event schedule is too large to retain in this "
                "conversation."
            ),
        }
    return report.page(
        phase=arguments.get("phase", "all"),
        event_type=arguments.get("event_type", "all"),
        limit=arguments.get("limit", 25),
    )


async def read_event_schedule_report(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    require_access_requirements(
        context.guild, context.member.id, {ACCESS_LEAD},
    )
    context.state.required_access.add(ACCESS_LEAD)
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if (
        not isinstance(report, EventScheduleReport)
        or report.guild_id != context.guild.id
    ):
        return {
            "error": (
                "That event schedule is not available in this conversation."
            ),
        }
    try:
        result = report.page(
            phase=arguments.get("phase", "all"),
            event_type=arguments.get("event_type", "all"),
            offset=arguments.get("offset", 0),
            limit=arguments.get("limit", 25),
        )
    except ValueError as error:
        return {"error": str(error)}
    require_access_requirements(
        context.guild, context.member.id, {ACCESS_LEAD},
    )
    await require_evidence_access(context)
    return result


__all__ = ["event_tools", "read_event_schedule", "read_event_schedule_report"]
