"""Read-only achievement tools over the feature-owned query interface."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping
from uuid import uuid4
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ...engine.capability_contract import CapabilityContract
from ...access import require_evidence_access
from .report import AchievementProgressReport
from ...reports.base import ArtifactCapacityError, retain_report
from ...models import AgentRequestContext, RegisteredAgentTool
TOOL_CONTRACTS = {
    "read_member_achievements": CapabilityContract(
        entity_fields=(("member_id", "discord_member"),),
        filter_fields=("status",),
    ),
    "read_member_achievement_report": CapabilityContract(
        entity_fields=(("report_id", "achievement_progress_report"),),
        filter_fields=("status",),
        retained_fields=("report_id",),
    ),
}


def achievement_tools() -> tuple[RegisteredAgentTool, ...]:
    report_id = {"type": "string", "minLength": 1, "maxLength": 32}
    offset = {"type": "integer", "minimum": 0}
    limit = {"type": "integer", "minimum": 1, "maximum": 25}
    definitions = (
        (
            "read_member_achievements",
            "Read and retain complete achievement progress for one current Discord member "
            "using the existing achievement rules. Completion-only achievements are not "
            "presented as measurable counters. This does not expose coin transactions or "
            "change achievements.",
            {
                "member_id": {"type": "integer", "minimum": 1},
                "status": {"type": "string", "enum": ["all", "completed", "in_progress"]},
                "limit": limit,
            },
            ("member_id",),
            read_member_achievements,
        ),
        (
            "read_member_achievement_report",
            "Read another filtered page from one retained member achievement snapshot "
            "without rereading mutable progress.",
            {
                "report_id": report_id,
                "status": {"type": "string", "enum": ["all", "completed", "in_progress"]},
                "offset": offset,
                "limit": limit,
            },
            ("report_id",),
            read_member_achievement_report,
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
        contract=TOOL_CONTRACTS[name],
    ) for name, description, properties, required, handler in definitions)


async def read_member_achievements(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.achievement_queries is None:
        return {"error": "Achievement data is not available."}
    member_id = arguments["member_id"]
    member = context.guild.get_member(member_id)
    if member is None:
        return {"error": "That member is not currently in this server."}
    try:
        snapshot = await asyncio.to_thread(
            context.achievement_queries.member_progress,
            member_id, joined_at=member.joined_at,
        )
    except (RuntimeError, ValueError):
        return {"error": "Achievement progress could not be read completely."}
    await require_evidence_access(context)
    current_member = context.guild.get_member(member_id)
    if current_member is None:
        return {"error": "That member is not currently in this server."}
    try:
        report = AchievementProgressReport(
            uuid4().hex, context.guild.id,
            current_member.display_name, snapshot,
        )
    except ValueError:
        return {"error": "Achievement progress could not be read completely."}
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {
            "error": (
                "The complete achievement report is too large to retain in "
                "this conversation."
            ),
        }
    return report.page(
        status=arguments.get("status", "all"),
        limit=arguments.get("limit", 25),
    )


async def read_member_achievement_report(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if (
        not isinstance(report, AchievementProgressReport)
        or report.guild_id != context.guild.id
    ):
        return {
            "error": (
                "That member achievement report is not available in this "
                "conversation."
            ),
        }
    try:
        result = report.page(
            status=arguments.get("status", "all"),
            offset=arguments.get("offset", 0),
            limit=arguments.get("limit", 25),
        )
    except ValueError as error:
        return {"error": str(error)}
    await require_evidence_access(context)
    return result
__all__ = ["achievement_tools", "read_member_achievement_report", "read_member_achievements"]
