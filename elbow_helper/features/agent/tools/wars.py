"""Read-only tools for current and previous regular-war evidence."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any, Mapping
from uuid import uuid4

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.features.account_links.evidence import account_ownership_evidence
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..reports.base import ArtifactCapacityError, retain_report
from ..models import AgentRequestContext, RegisteredAgentTool
from ..reports.historical_war import HistoricalRegularWarReport, HistoricalWarOwnedMember
from ..reports.war import RegularWarReport


def war_tools() -> tuple[RegisteredAgentTool, ...]:
    definitions = (
        ("list_regular_war_status",
         "List the latest regular-war evidence status for each supported family clan without polling Clash. Observed, cached, not-in-war, CWL-active and unavailable states remain distinct.",
         {}, (), list_regular_war_status),
        ("read_regular_war",
         "Read and retain the current or previous regular-war snapshot for one family clan without refreshing it. Current evidence reports its observation status; ended wars include deterministic missed-attack counts.",
         {"clan_code": {"type": "string", "description": "Brown Elbow clan code such as BEH or BE4."},
          "selected": {"type": "string", "enum": ["current", "previous"], "default": "current"}},
         ("clan_code",), read_regular_war),
        ("read_regular_war_report",
         "Read another roster page from a retained regular-war report without polling Clash or rereading mutable war state.",
         {"report_id": {"type": "string", "maxLength": 32},
          "offset": {"type": "integer", "minimum": 0},
          "limit": {"type": "integer", "minimum": 1, "maximum": 25}},
         ("report_id",), read_regular_war_report),
        ("read_historical_regular_wars",
         "Read and retain a bounded page of completed regular wars from Clan Health storage, including final roster and missed-attack evidence. Current account links are joined in one separately dated snapshot and do not prove historical ownership. This does not refresh war or account data.",
         {"clan_code": {"type": "string", "description": "Brown Elbow clan code such as BEH or BE4."},
          "history_limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
          "before_war_id": {"type": "string", "minLength": 1, "maxLength": 300}},
         ("clan_code",), read_historical_regular_wars),
        ("read_historical_regular_war_report",
         "Read member summaries or account-war rows from a retained historical regular-war report without repeating database reads. Optional exact account-tag or current-member filters apply only to war rows.",
         {"report_id": {"type": "string", "maxLength": 32},
          "view": {"type": "string", "enum": ["member_summaries", "war_rows"],
                   "default": "member_summaries"},
          "offset": {"type": "integer", "minimum": 0},
          "limit": {"type": "integer", "minimum": 1, "maximum": 25},
          "player_tag": {"type": "string"},
          "member_id": {"type": "integer", "minimum": 1}},
         ("report_id",), read_historical_regular_war_report),
    )
    return tuple(RegisteredAgentTool(AgentToolDefinition(
        name=name, description=description,
        parameters={"type": "object", "properties": properties,
                    "required": list(required), "additionalProperties": False},
    ), handler) for name, description, properties, required, handler in definitions)


async def list_regular_war_status(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.war_queries is None:
        return {"error": "Regular-war evidence is not available."}
    statuses = context.war_queries.statuses()
    await require_evidence_access(context)
    return {"clans": [asdict(status) for status in statuses], "poll_performed": False}


async def read_regular_war(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.war_queries is None:
        return {"error": "Regular-war evidence is not available."}
    clan_code = str(arguments.get("clan_code") or "").strip().upper()
    if clan_code not in context.war_queries.supported_clan_codes:
        return {
            "error": "Regular-war evidence is not available for that clan code.",
            "known_clan_codes": list(context.war_queries.supported_clan_codes),
        }
    try:
        snapshot = context.war_queries.snapshot(
            clan_code, selected=arguments.get("selected", "current"),
        )
    except ValueError:
        return {"error": "Stored regular-war evidence for that clan could not be read."}
    await require_evidence_access(context)
    if snapshot.war_id is None:
        return {
            "clan_code": clan_code, "selected": snapshot.selected,
            "evidence_status": snapshot.evidence_status,
            "observed_at": snapshot.observed_at, "report_id": None,
            "poll_performed": False,
        }
    report = RegularWarReport(uuid4().hex, context.guild.id, snapshot)
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {"error": "The complete regular-war report is too large to retain in this conversation."}
    return report.page()


async def read_regular_war_report(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if not isinstance(report, RegularWarReport) or report.guild_id != context.guild.id:
        return {"error": "That regular-war report is not available in this conversation. Read the war again."}
    result = report.page(offset=arguments.get("offset", 0), limit=arguments.get("limit", 25))
    await require_evidence_access(context)
    return result


async def read_historical_regular_wars(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    clan_code = str(arguments.get("clan_code") or "").strip().upper()
    if clan_code not in CLANS:
        return {"error": "Unknown Brown Elbow clan code.", "known_clan_codes": sorted(CLANS)}
    try:
        history = await context.clan_health.regular_war_history(
            clan_code, history_limit=arguments.get("history_limit", 10),
            before_war_id=arguments.get("before_war_id"),
        )
    except ValueError:
        return {"error": "Stored regular-war history for that clan could not be read."}
    await require_evidence_access(context)
    if not history.wars:
        return {
            "clan_code": clan_code, "war_count": 0,
            "next_before_war_id": None, "report_id": None,
            "complete_selected_history": True,
        }
    tags = tuple(dict.fromkeys(row.player_tag for row in history.members))
    ownership = await asyncio.to_thread(
        account_ownership_evidence, context.account_links, tags,
    )
    await require_evidence_access(context)
    ownership_by_tag = {row.player_tag: row for row in ownership.accounts}
    if set(ownership_by_tag) != set(tags):
        return {"error": "Current account ownership evidence is incomplete."}
    try:
        report = HistoricalRegularWarReport(
            uuid4().hex, context.guild.id, ownership.observed_at, history,
            tuple(HistoricalWarOwnedMember(
                source=row,
                linked_member_id=ownership_by_tag[row.player_tag].linked_member_id,
                linked_player_name=ownership_by_tag[row.player_tag].linked_player_name,
            ) for row in history.members),
        )
    except ValueError:
        return {"error": "Stored regular-war history for that clan could not be read."}
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {"error": "The complete historical regular-war report is too large to retain in this conversation."}
    return report.page()


async def read_historical_regular_war_report(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if not isinstance(report, HistoricalRegularWarReport) or report.guild_id != context.guild.id:
        return {"error": "That historical regular-war report is not available in this conversation. Read the history again."}
    player_tag = None
    if "player_tag" in arguments:
        player_tag = normalize_player_tag(arguments["player_tag"])
        if player_tag is None:
            return {"error": "A valid Clash player tag is required."}
    member_id = arguments.get("member_id")
    if member_id is not None and (type(member_id) is not int or member_id <= 0):
        return {"error": "A valid Discord member ID is required."}
    view = arguments.get("view", "member_summaries")
    if view != "war_rows" and (player_tag is not None or member_id is not None):
        return {"error": "Account and member filters require the war_rows view."}
    try:
        result = report.page(
            view=view,
            offset=arguments.get("offset", 0), limit=arguments.get("limit", 25),
            player_tag=player_tag, member_id=member_id,
        )
    except ValueError as error:
        return {"error": str(error)}
    await require_evidence_access(context)
    return result


__all__ = ["war_tools"]
