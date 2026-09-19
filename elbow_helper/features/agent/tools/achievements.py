"""Read-only achievement tools over the feature-owned query interface."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping
from uuid import uuid4

from elbow_helper.features.achievements.queries import (
    achievement_leaderboard_eligible,
)
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..reports.achievement import (
    AchievementLeaderboardMember, AchievementLeaderboardReport,
    AchievementProgressReport,
)
from ..reports.base import ArtifactCapacityError, retain_report
from ..models import AgentRequestContext, RegisteredAgentTool


def achievement_tools() -> tuple[RegisteredAgentTool, ...]:
    report_id = {"type": "string", "minLength": 1, "maxLength": 32}
    offset = {"type": "integer", "minimum": 0}
    limit = {"type": "integer", "minimum": 1, "maximum": 25}
    definitions = (
        (
            "read_member_achievements",
            "Read and retain complete achievement progress for one current Discord member using the existing achievement rules. Completion-only achievements are not presented as measurable counters. This does not expose coin transactions or change achievements.",
            {
                "member_id": {"type": "integer", "minimum": 1},
                "status": {
                    "type": "string",
                    "enum": ["all", "completed", "in_progress"],
                },
                "limit": limit,
            },
            ("member_id",),
            read_member_achievements,
        ),
        (
            "read_member_achievement_report",
            "Read another filtered page from one retained member achievement snapshot without rereading mutable progress.",
            {
                "report_id": report_id,
                "status": {
                    "type": "string",
                    "enum": ["all", "completed", "in_progress"],
                },
                "offset": offset, "limit": limit,
            },
            ("report_id",),
            read_member_achievement_report,
        ),
        (
            "read_achievement_leaderboard",
            "Read and retain complete achievement counts for current non-Lead server members who have earned at least one achievement, matching the existing member leaderboard scope. This is stored achievement evidence, not an activity, value or leadership ranking, and performs no economy or raffle action.",
            {"limit": limit},
            (),
            read_achievement_leaderboard,
        ),
        (
            "read_achievement_leaderboard_report",
            "Read another page from one retained achievement-count leaderboard without recalculating against mutable member or achievement state.",
            {"report_id": report_id, "offset": offset, "limit": limit},
            ("report_id",),
            read_achievement_leaderboard_report,
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


async def read_achievement_leaderboard(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.achievement_queries is None:
        return {"error": "Achievement data is not available."}
    try:
        snapshot = await asyncio.to_thread(
            context.achievement_queries.leaderboard_counts,
        )
    except (RuntimeError, ValueError):
        return {"error": "Achievement counts could not be read completely."}
    await require_evidence_access(context)
    try:
        rows = tuple(
            AchievementLeaderboardMember(
                row.member_id, member.display_name, row.achievement_count,
            )
            for row in snapshot.rows
            if (member := context.guild.get_member(row.member_id)) is not None
            and achievement_leaderboard_eligible({
                role.id for role in member.roles
            })
        )
        report = AchievementLeaderboardReport(
            uuid4().hex, context.guild.id, snapshot.observed_at,
            snapshot.total_achievements, rows,
        )
    except ValueError:
        return {"error": "Achievement counts could not be read completely."}
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {
            "error": (
                "The complete achievement leaderboard is too large to retain "
                "in this conversation."
            ),
        }
    return report.page(limit=arguments.get("limit", 25))


async def read_achievement_leaderboard_report(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if (
        not isinstance(report, AchievementLeaderboardReport)
        or report.guild_id != context.guild.id
    ):
        return {
            "error": (
                "That achievement leaderboard is not available in this "
                "conversation."
            ),
        }
    try:
        result = report.page(
            offset=arguments.get("offset", 0),
            limit=arguments.get("limit", 25),
        )
    except ValueError as error:
        return {"error": str(error)}
    await require_evidence_access(context)
    return result


__all__ = [
    "achievement_tools", "read_achievement_leaderboard",
    "read_achievement_leaderboard_report", "read_member_achievement_report",
    "read_member_achievements",
]
