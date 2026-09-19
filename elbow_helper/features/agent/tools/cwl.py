"""Read-only CWL performance and registered-thread evidence."""

import asyncio
from dataclasses import asdict
from typing import Any, Mapping
from uuid import uuid4

from elbow_helper.configuration.clans import CLAN_ORDER
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import accessible_message_channel, require_evidence_access
from ..reports.base import ArtifactCapacityError, retain_report
from ..reports.cwl import CwlPerformanceReport
from ..models import AgentRequestContext, RegisteredAgentTool
from .cwl_scoring import cwl_scoring_tools


def cwl_tools() -> tuple[RegisteredAgentTool, ...]:
    offset = {"type": "integer", "minimum": 0}
    limit = {"type": "integer", "minimum": 1, "maximum": 25}
    filters = {
        "season": {"type": "string", "maxLength": 20},
        "clan_code": {"type": "string", "enum": list(CLAN_ORDER)},
        "player_tag": {"type": "string", "maxLength": 20},
        "offset": offset, "limit": limit,
    }
    definitions = (
        ("read_cwl_performance", "Read completed historical CWL performance using the established ASS profiles. Results retain one complete dated snapshot; use read_cwl_performance_report for more pages or filters. Missing attacks and missing data are different.",
         {"history_limit": {"type": "integer", "minimum": 1, "maximum": 12}, **filters}, (), read_cwl_performance),
        ("read_cwl_performance_report", "Read another page or filter of a retained CWL performance report from this conversation without repeating the database lookup.",
         {"report_id": {"type": "string", "maxLength": 32}, **filters}, ("report_id",), read_cwl_performance_report),
        ("read_cwl_threads", "List registered CWL discussion threads the requester and bot can currently access. These are owner-registered targets, not threads inferred from names.",
         {}, (), read_cwl_threads),
    )
    return tuple(RegisteredAgentTool(AgentToolDefinition(
        name=name, description=description,
        parameters={"type": "object", "properties": properties,
                    "required": list(required), "additionalProperties": False},
    ), handler) for name, description, properties, required, handler in definitions) + cwl_scoring_tools()


async def read_cwl_performance(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.cwl_queries is None:
        return {"error": "CWL performance data is not available."}
    if arguments.get("player_tag") is not None and normalize_player_tag(arguments["player_tag"]) is None:
        return {"error": "That player tag is not valid."}
    snapshot = await asyncio.to_thread(
        context.cwl_queries.performance, history_limit=arguments.get("history_limit", 3),
    )
    await require_evidence_access(context)
    report = CwlPerformanceReport(uuid4().hex, context.guild.id, snapshot)
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {"error": "The complete CWL performance report is too large to retain in this conversation."}
    return _report_page(report, arguments)


async def read_cwl_performance_report(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if not isinstance(report, CwlPerformanceReport) or report.guild_id != context.guild.id:
        return {"error": "That CWL performance report is not available in this conversation. Read the performance data again."}
    result = _report_page(report, arguments)
    await require_evidence_access(context)
    return result


async def read_cwl_threads(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.cwl_queries is None:
        return {"error": "CWL thread registrations are not available."}
    registrations = context.cwl_queries.registered_threads()
    accessible = []
    for registration in registrations:
        if await accessible_message_channel(context, registration.thread_id) is None:
            continue
        accessible.append({
            **asdict(registration),
            "url": f"https://discord.com/channels/{context.guild.id}/{registration.thread_id}",
        })
        context.state.source_channels.add(registration.thread_id)
    await require_evidence_access(context)
    return {
        "threads": accessible,
        "accessible_count": len(accessible),
        "omitted_inaccessible_count": len(registrations) - len(accessible),
    }


def _report_page(report: CwlPerformanceReport, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    player_tag = None
    if arguments.get("player_tag") is not None:
        player_tag = normalize_player_tag(arguments["player_tag"])
        if player_tag is None:
            return {"error": "That player tag is not valid."}
    return report.page(
        season=arguments.get("season"), clan_code=arguments.get("clan_code"),
        player_tag=player_tag, offset=arguments.get("offset", 0),
        limit=arguments.get("limit", 25),
    )
