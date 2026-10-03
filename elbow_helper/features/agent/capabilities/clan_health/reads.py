"""Read-only access to stored clan-health meaning and comparisons."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any, Mapping
from uuid import uuid4

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.features.account_links.evidence import account_ownership_evidence
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ...engine.capability_contract import CapabilityContract
from ...access import lookup_level, ACCESS_LEAD_PLUS
from ...access import require_evidence_access
from ...reports.base import ArtifactCapacityError, retain_report
from .report import ClanHealthReport, compare_clan_health_reports as compare_reports
from .movement_report import (
    FamilyMovementReport, OwnedFamilyAccountMovement, TRANSITIONS,
    movement_history_manifest,
)
from ...models import AgentRequestContext, RegisteredAgentTool
from ..validation import bounded_int


HEALTH_PLAYER_RESULT_LIMIT = 10
HEALTH_WINDOW_DEFAULT_DAYS = 30
HEALTH_WINDOW_MAX_DAYS = 365


TOOL_CONTRACTS = {
    'find_clan_health_players': CapabilityContract(
        entity_fields=(('clan_code', 'clan'),),
        time_fields=(),
        required_access=frozenset({ACCESS_LEAD_PLUS}),
    ),
    'get_player_health': CapabilityContract(
        entity_fields=(('player_tag', 'clash_account'),),
        time_fields=('days',),
        latest_fields=('days',),
        required_access=frozenset({ACCESS_LEAD_PLUS}),
    ),
    'list_clan_health_reports': CapabilityContract(
        entity_fields=(('clan_code', 'clan'),),
        time_fields=('before_run_id',),
        latest_fields=('before_run_id',),
        result_paths=(('reports', 'N', 'run_id'),),
        period_results=(('reports', 0, 'run_id'),),
        required_access=frozenset({ACCESS_LEAD_PLUS}),
    ),
    'get_clan_health': CapabilityContract(
        entity_fields=(('clan_code', 'clan'),),
        time_fields=(),
        required_access=frozenset({ACCESS_LEAD_PLUS}),
    ),
    'read_clan_health_period': CapabilityContract(
        entity_fields=(('clan_code', 'clan'), ('run_id', 'clan_health_run')),
        time_fields=(),
        required_access=frozenset({ACCESS_LEAD_PLUS}),
    ),
    'read_clan_health_report': CapabilityContract(
        entity_fields=(('report_id', 'clan_health_report'),),
        time_fields=(),
        retained_fields=('report_id',),
        required_access=frozenset({ACCESS_LEAD_PLUS}),
    ),
    'compare_clan_health_reports': CapabilityContract(
        entity_fields=(('before_report_id', 'clan_health_report'), ('after_report_id', 'clan_health_report')),
        time_fields=(),
        retained_fields=('before_report_id', 'after_report_id'),
        required_access=frozenset({ACCESS_LEAD_PLUS}),
    ),
    'read_family_account_movements': CapabilityContract(
        entity_fields=(),
        time_fields=('before_run_id', 'interval_limit'),
        latest_fields=('before_run_id', 'interval_limit'),
        required_access=frozenset({ACCESS_LEAD_PLUS}),
    ),
    'read_family_account_movement_report': CapabilityContract(
        entity_fields=(('report_id', 'family_movement_report'), ('player_tag', 'clash_account'), ('member_id', 'discord_member'), ('clan_code', 'clan')),
        time_fields=(),
        filter_fields=('view', 'transition'),
        retained_fields=('report_id',),
        required_access=frozenset({ACCESS_LEAD_PLUS}),
    ),
}

def clan_health_tools() -> tuple[RegisteredAgentTool, ...]:
    offset = {"type": "integer", "minimum": 0}
    page_limit = {"type": "integer", "minimum": 1, "maximum": 25}
    report_id = {"type": "string", "maxLength": 32}
    run_id = {"type": "string", "minLength": 1, "maxLength": 100}
    clan_code = {
        "type": "string",
        "description": "Brown Elbow clan code such as BEH or BE4.",
    }
    definitions = (
        ("find_clan_health_players",
         "Search the last-seen Clan Health player directory by name, tag, or clan-code prefix. An exact clan filter and result offset narrow the source read. The returned last_seen_ts is an observation time, not proof of current clan membership.",
         {"query": {"type": "string", "minLength": 1, "maxLength": 100},
          "clan_code": {"type": "string", "enum": sorted(CLANS)},
          "offset": {"type": "integer", "minimum": 0, "maximum": 10000},
          "limit": {"type": "integer", "minimum": 1, "maximum": 25}},
         ("query",), find_clan_health_players),
        ("get_player_health",
         "Get stored activity, war, raid, progression, movement, and report evidence for one exact Clash player tag over a bounded recent window.",
         {"player_tag": {"type": "string", "description": "Exact Clash player tag returned by another tool."},
          "days": {"type": "integer", "minimum": 7, "maximum": HEALTH_WINDOW_MAX_DAYS,
                   "default": HEALTH_WINDOW_DEFAULT_DAYS}},
         ("player_tag",), get_player_health),
        ("list_clan_health_reports",
         "List completed stored clan-health reporting periods for one family clan. Each period resolves to its latest complete run; use the exact run ID to read historical evidence.",
         {"clan_code": clan_code,
          "before_run_id": run_id,
          "limit": {"type": "integer", "minimum": 1, "maximum": 25}},
         ("clan_code",), list_clan_health_reports),
        ("get_clan_health",
         "Get and retain the latest completed stored clan-health report for one Brown Elbow family clan. Counts and totals cover the complete report; use read_clan_health_report for more player rows.",
         {"clan_code": clan_code}, ("clan_code",), get_clan_health),
        ("read_clan_health_period",
         "Read and retain one exact completed clan-health run returned by list_clan_health_reports. This uses stored evidence and does not refresh clan data.",
         {"clan_code": clan_code, "run_id": run_id},
         ("clan_code", "run_id"), read_clan_health_period),
        ("read_clan_health_report",
         "Read another page of a retained clan-health report without repeating the database lookup.",
         {"report_id": report_id, "offset": offset, "limit": page_limit},
         ("report_id",), read_clan_health_report),
        ("compare_clan_health_reports",
         "Compare two retained completed clan-health reports for the same clan by exact account tag. Counts and aggregate deltas cover both complete reports; pages show changed, added and removed report rows. Presence changes do not by themselves prove a clan movement or inactivity.",
         {"before_report_id": report_id, "after_report_id": report_id,
          "offset": offset, "limit": page_limit},
         ("before_report_id", "after_report_id"), compare_clan_health_reports),
        ("read_family_account_movements",
         "Read and retain observed account-presence changes between consecutive complete Clan Health family snapshots. Changes show family-clan observations, not transfer intent or exact movement time. Current account links are joined separately and do not prove historical ownership.",
         {"interval_limit": {"type": "integer", "minimum": 1, "maximum": 20,
                             "default": 10},
          "before_run_id": run_id},
         (), read_family_account_movements),
        ("read_family_account_movement_report",
         "Read current-owner summaries or observed movement rows from a retained family movement report without repeating database reads. Exact account, current-member, transition and clan filters apply only to movement rows.",
         {"report_id": report_id,
          "view": {"type": "string", "enum": ["owner_summaries", "movements"],
                   "default": "owner_summaries"},
          "offset": offset, "limit": page_limit,
          "player_tag": {"type": "string"},
          "member_id": {"type": "integer", "minimum": 1},
          "transition": {"type": "string", "enum": sorted(TRANSITIONS)},
          "clan_code": clan_code},
         ("report_id",), read_family_account_movement_report),
    )
    return tuple(RegisteredAgentTool(AgentToolDefinition(
        name=name, description=description,
        parameters={"type": "object", "properties": properties,
                    "required": list(required), "additionalProperties": False},
    ), handler,
        contract=TOOL_CONTRACTS[name],
    ) for name, description, properties, required, handler in definitions)


@lookup_level(ACCESS_LEAD_PLUS)
async def find_clan_health_players(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    query = str(arguments.get("query") or "").strip()
    if not query:
        return {"error": "A player name, tag, or clan code is required."}
    clan_code = None
    if "clan_code" in arguments:
        clan_code = _clan_code(arguments)
        if clan_code is None:
            return _unknown_clan()
    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", HEALTH_PLAYER_RESULT_LIMIT)
    rows = await context.clan_health.search_players(
        query, limit=limit + 1, offset=offset, clan_code=clan_code,
    )
    await require_evidence_access(context)
    return {
        "query": query, "clan_code": clan_code,
        "players": list(rows[:limit]),
        "next_offset": offset + limit if len(rows) > limit else None,
    }


@lookup_level(ACCESS_LEAD_PLUS)
async def get_player_health(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    player_tag = normalize_player_tag(str(arguments.get("player_tag") or ""))
    if not player_tag:
        return {"error": "A valid Clash player tag is required."}
    days = bounded_int(
        arguments.get("days"), default=HEALTH_WINDOW_DEFAULT_DAYS,
        minimum=7, maximum=HEALTH_WINDOW_MAX_DAYS,
    )
    result = await context.clan_health.player_health(player_tag, days=days)
    await require_evidence_access(context)
    return result


@lookup_level(ACCESS_LEAD_PLUS)
async def list_clan_health_reports(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    clan_code = _clan_code(arguments)
    if clan_code is None:
        return _unknown_clan()
    requested_limit = arguments.get("limit", 10)
    runs = await context.clan_health.report_runs(
        clan_code, before_run_id=arguments.get("before_run_id"),
        limit=requested_limit + 1,
    )
    page, has_more = runs[:requested_limit], len(runs) > requested_limit
    await require_evidence_access(context)
    return {
        "clan_code": clan_code, "reports": [asdict(run) for run in page],
        "next_before_run_id": page[-1].run_id if has_more else None,
        "order": "newest_completed_period_first", "complete_runs_only": True,
    }


@lookup_level(ACCESS_LEAD_PLUS)
async def get_clan_health(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    clan_code = _clan_code(arguments)
    if clan_code is None:
        return _unknown_clan()
    snapshot = await context.clan_health.latest_report(clan_code)
    if snapshot is None:
        return {"error": "No completed clan-health report is stored for that clan."}
    await require_evidence_access(context)
    return _retain_snapshot(context, snapshot)


@lookup_level(ACCESS_LEAD_PLUS)
async def read_clan_health_period(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    clan_code = _clan_code(arguments)
    if clan_code is None:
        return _unknown_clan()
    snapshot = await context.clan_health.report(clan_code, arguments["run_id"])
    if snapshot is None:
        return {"error": "That completed clan-health report is not available for this clan."}
    await require_evidence_access(context)
    return _retain_snapshot(context, snapshot)


@lookup_level(ACCESS_LEAD_PLUS)
async def read_clan_health_report(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if not isinstance(report, ClanHealthReport) or report.guild_id != context.guild.id:
        return {"error": "That clan-health report is not available in this conversation. Read the period again."}
    result = report.page(offset=arguments.get("offset", 0), limit=arguments.get("limit", 25))
    await require_evidence_access(context)
    return result


@lookup_level(ACCESS_LEAD_PLUS)
async def compare_clan_health_reports(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    before = context.state.reports.get(arguments["before_report_id"])
    after = context.state.reports.get(arguments["after_report_id"])
    if any(
        not isinstance(report, ClanHealthReport) or report.guild_id != context.guild.id
        for report in (before, after)
    ):
        return {"error": "Both clan-health reports must be available in this conversation and server."}
    try:
        result = compare_reports(
            before, after, offset=arguments.get("offset", 0),
            limit=arguments.get("limit", 25),
        )
    except ValueError as error:
        return {"error": str(error)}
    await require_evidence_access(context)
    return result


@lookup_level(ACCESS_LEAD_PLUS)
async def read_family_account_movements(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    try:
        history = await context.clan_health.family_movement_history(
            interval_limit=arguments.get("interval_limit", 10),
            before_run_id=arguments.get("before_run_id"),
        )
    except ValueError:
        return {"error": "Stored complete family snapshots could not be read."}
    await require_evidence_access(context)
    if not history.movements:
        return movement_history_manifest(history)
    tags = tuple(dict.fromkeys(row.player_tag for row in history.movements))
    ownership = await asyncio.to_thread(
        account_ownership_evidence, context.account_links, tags,
    )
    await require_evidence_access(context)
    ownership_by_tag = {row.player_tag: row for row in ownership.accounts}
    if set(ownership_by_tag) != set(tags):
        return {"error": "Current account ownership evidence is incomplete."}
    try:
        report = FamilyMovementReport(
            uuid4().hex, context.guild.id, ownership.observed_at, history,
            tuple(OwnedFamilyAccountMovement(
                source=row,
                linked_member_id=ownership_by_tag[row.player_tag].linked_member_id,
                linked_player_name=ownership_by_tag[row.player_tag].linked_player_name,
            ) for row in history.movements),
        )
    except ValueError:
        return {"error": "Stored complete family snapshots could not be read."}
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {"error": "The complete family movement report is too large to retain in this conversation."}
    return report.page()


@lookup_level(ACCESS_LEAD_PLUS)
async def read_family_account_movement_report(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if not isinstance(report, FamilyMovementReport) or report.guild_id != context.guild.id:
        return {"error": "That family movement report is not available in this conversation. Read the snapshots again."}
    player_tag = None
    if "player_tag" in arguments:
        player_tag = normalize_player_tag(arguments["player_tag"])
        if player_tag is None:
            return {"error": "A valid Clash player tag is required."}
    member_id = arguments.get("member_id")
    if member_id is not None and (type(member_id) is not int or member_id <= 0):
        return {"error": "A valid Discord member ID is required."}
    transition = arguments.get("transition")
    if transition is not None and (
        not isinstance(transition, str) or transition not in TRANSITIONS
    ):
        return {"error": "Unknown family movement transition."}
    clan_code = None
    if "clan_code" in arguments:
        clan_code = _clan_code(arguments)
        if clan_code is None:
            return _unknown_clan()
    view = arguments.get("view", "owner_summaries")
    if view != "movements" and any(
        value is not None for value in (player_tag, member_id, transition, clan_code)
    ):
        return {"error": "Account, member, transition and clan filters require the movements view."}
    try:
        result = report.page(
            view=view, offset=arguments.get("offset", 0),
            limit=arguments.get("limit", 25), player_tag=player_tag,
            member_id=member_id, transition=transition, clan_code=clan_code,
        )
    except ValueError as error:
        return {"error": str(error)}
    await require_evidence_access(context)
    return result


def _retain_snapshot(context: AgentRequestContext, snapshot: Any) -> Mapping[str, Any]:
    report = ClanHealthReport(uuid4().hex, context.guild.id, snapshot)
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {"error": "The complete clan-health report is too large to retain in this conversation."}
    return report.page()


def _clan_code(arguments: Mapping[str, Any]) -> str | None:
    value = str(arguments.get("clan_code") or "").strip().upper()
    return value if value in CLANS else None


def _unknown_clan() -> Mapping[str, Any]:
    return {"error": "Unknown Brown Elbow clan code.", "known_clan_codes": sorted(CLANS)}
