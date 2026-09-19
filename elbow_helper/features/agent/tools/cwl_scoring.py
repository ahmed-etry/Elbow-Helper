"""Thin agent adapters for feature-owned CWL scoring evidence."""

from __future__ import annotations

import asyncio
from typing import Any, Mapping
from uuid import uuid4

from elbow_helper.features.cwl.config import CWL_CLAN_CODES
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..reports.base import ArtifactCapacityError, retain_report
from ..reports.cwl_bonus import CwlBonusScopeReport
from ..reports.cwl import CwlAssScopeReport
from ..models import AgentRequestContext, RegisteredAgentTool


def cwl_scoring_tools() -> tuple[RegisteredAgentTool, ...]:
    offset = {"type": "integer", "minimum": 0}
    limit = {"type": "integer", "minimum": 1, "maximum": 25}
    definitions = (
        (
            "list_cwl_ass_seasons",
            "List seasons with stored completed CWL wars for one clan so an "
            "unfamiliar ASS request can resolve its exact scope. This does not "
            "calculate a score or poll Clash.",
            {"clan_code": {
                "type": "string", "enum": list(CWL_CLAN_CODES),
            }},
            ("clan_code",), list_cwl_ass_seasons,
        ),
        (
            "read_cwl_ass_scope",
            "Calculate ASS from the exact selected stored CWL season, day "
            "(round) or war using the established scoring implementation. "
            "Observed attack averages are projected to seven attacks; preserve "
            "the returned scope and sample size and do not present a "
            "partial-scope result as a completed-season score. Retain the "
            "complete result for paging or a requested spreadsheet.",
            {
                "clan_code": {
                    "type": "string", "enum": list(CWL_CLAN_CODES),
                },
                "season": {
                    "type": "string",
                    "pattern": r"^20\d{2}-(0[1-9]|1[0-2])$",
                },
                "scope_type": {
                    "type": "string", "enum": ["season", "round", "war"],
                },
                "cwl_round": {
                    "type": "integer", "minimum": 1, "maximum": 7,
                },
                "war_id": {
                    "type": "string", "minLength": 1, "maxLength": 100,
                },
                "limit": limit,
            },
            ("clan_code", "season", "scope_type"), read_cwl_ass_scope,
        ),
        (
            "read_cwl_ass_scope_report",
            "Read another page from one retained scoped ASS result without "
            "rereading mutable history. Preserve its selected scope, observed "
            "attack sample and seven-attack projection when explaining or "
            "preparing a spreadsheet.",
            {
                "report_id": {"type": "string", "maxLength": 32},
                "offset": offset, "limit": limit,
            },
            ("report_id",), read_cwl_ass_scope_report,
        ),
        (
            "read_cwl_bonus_scope",
            "Apply the clan's existing configured CWL bonus scoring to stored "
            "completed attacks for one season, round or exact war. This returns "
            "adjusted-delta evidence, not ASS, and does not poll Clash or publish "
            "a bonus recommendation. Retain the complete result for paging or a "
            "requested spreadsheet.",
            {
                "clan_code": {
                    "type": "string", "enum": list(CWL_CLAN_CODES),
                },
                "season": {
                    "type": "string",
                    "pattern": r"^20\d{2}-(0[1-9]|1[0-2])$",
                },
                "scope_type": {
                    "type": "string", "enum": ["season", "round", "war"],
                },
                "cwl_round": {
                    "type": "integer", "minimum": 1, "maximum": 7,
                },
                "war_tag": {
                    "type": "string", "minLength": 1, "maxLength": 100,
                },
                "limit": limit,
            },
            ("clan_code", "season", "scope_type"), read_cwl_bonus_scope,
        ),
        (
            "read_cwl_bonus_scope_report",
            "Read another page from one retained configured CWL bonus-scoring "
            "result without recalculating against mutable settings or history. "
            "Keep this metric distinct from ASS.",
            {
                "report_id": {"type": "string", "maxLength": 32},
                "offset": offset, "limit": limit,
            },
            ("report_id",), read_cwl_bonus_scope_report,
        ),
    )
    return tuple(RegisteredAgentTool(
        AgentToolDefinition(
            name=name, description=description,
            parameters={
                "type": "object", "properties": properties,
                "required": list(required), "additionalProperties": False,
            },
        ),
        handler,
    ) for name, description, properties, required, handler in definitions)


async def list_cwl_ass_seasons(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.cwl_queries is None:
        return {"error": "CWL performance data is not available."}
    try:
        seasons = await asyncio.to_thread(
            context.cwl_queries.ass_seasons,
            clan_code=arguments["clan_code"],
        )
    except ValueError as error:
        return {"error": str(error)}
    await require_evidence_access(context)
    return {
        "clan_code": arguments["clan_code"],
        "seasons": list(seasons), "season_count": len(seasons),
        "coverage": "stored_completed_cwl_wars",
    }


async def read_cwl_ass_scope(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.cwl_queries is None:
        return {"error": "CWL performance data is not available."}
    try:
        snapshot = await asyncio.to_thread(
            context.cwl_queries.ass_scope,
            clan_code=arguments["clan_code"], season=arguments["season"],
            scope_type=arguments["scope_type"],
            cwl_round=arguments.get("cwl_round"),
            war_id=arguments.get("war_id"),
        )
        report = CwlAssScopeReport(
            uuid4().hex, context.guild.id, snapshot,
        )
    except ValueError as error:
        return {"error": str(error)}
    await require_evidence_access(context)
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {
            "error": (
                "The complete scoped CWL ASS report is too large to retain "
                "in this conversation."
            ),
        }
    return report.page(limit=arguments.get("limit", 25))


async def read_cwl_ass_scope_report(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if (
        not isinstance(report, CwlAssScopeReport)
        or report.guild_id != context.guild.id
    ):
        return {
            "error": (
                "That scoped CWL ASS report is not available in this "
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


async def read_cwl_bonus_scope(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.cwl_queries is None:
        return {"error": "CWL bonus scoring is not available."}
    try:
        snapshot = await asyncio.to_thread(
            context.cwl_queries.bonus_scope,
            clan_code=arguments["clan_code"], season=arguments["season"],
            scope_type=arguments["scope_type"],
            cwl_round=arguments.get("cwl_round"),
            war_tag=arguments.get("war_tag"),
        )
        report = CwlBonusScopeReport(
            uuid4().hex, context.guild.id, snapshot,
        )
    except (RuntimeError, ValueError) as error:
        return {"error": str(error)}
    await require_evidence_access(context)
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {
            "error": (
                "The complete CWL bonus-scoring report is too large to retain "
                "in this conversation."
            ),
        }
    return report.page(limit=arguments.get("limit", 25))


async def read_cwl_bonus_scope_report(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if (
        not isinstance(report, CwlBonusScopeReport)
        or report.guild_id != context.guild.id
    ):
        return {
            "error": (
                "That CWL bonus-scoring report is not available in this "
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
    "cwl_scoring_tools", "list_cwl_ass_seasons", "read_cwl_ass_scope",
    "read_cwl_ass_scope_report", "read_cwl_bonus_scope",
    "read_cwl_bonus_scope_report",
]
