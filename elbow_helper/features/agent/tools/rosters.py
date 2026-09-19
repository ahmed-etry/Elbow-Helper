"""Read-only roster tools using the feature-owned query interface."""

from dataclasses import asdict
from typing import Any, Mapping
from uuid import uuid4

from elbow_helper.configuration.clans import CLANS
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import accessible_message_channel, require_evidence_access
from ..reports.base import ArtifactCapacityError, retain_report
from ..models import AgentRequestContext, RegisteredAgentTool
from ..reports.roster import RosterReport, compare_roster_reports as compare_reports, roster_summary


def roster_tools() -> tuple[RegisteredAgentTool, ...]:
    positive_id = {"type": "integer", "minimum": 1}
    offset = {"type": "integer", "minimum": 0}
    limit = {"type": "integer", "minimum": 1, "maximum": 25}
    definitions = (
        ("find_rosters", "Find rosters in this server by name or clan. Results identify the roster and its current signup cycle.",
         {"query": {"type": "string", "maxLength": 100}, "offset": offset}, (), find_rosters),
        ("list_roster_cycles", "List saved cycles for an identified roster. Use exact cycle IDs for historical membership; do not infer a season from a roster name.",
         {"roster_id": positive_id, "before_id": positive_id, "limit": limit}, ("roster_id",), list_roster_cycles),
        ("read_roster", "Read all stored signups for an identified roster and cycle. Omitting the cycle selects its current cycle. Roster settings and post locations are current, even when signups are historical. Stored signups do not prove current clan membership. Results retain a complete report; use read_roster_report for subsequent pages without repeating the lookup.",
         {"roster_id": positive_id, "cycle_id": positive_id}, ("roster_id",), read_roster),
        ("read_roster_report", "Read another page of a retained roster signup report from this conversation. Reuse its snapshot for comparisons; call read_roster again only when fresh data is needed.",
         {"report_id": {"type": "string", "maxLength": 32}, "offset": offset, "limit": limit}, ("report_id",), read_roster_report),
        ("compare_roster_reports", "Compare two retained roster reports by account tag. Supply before and after report IDs in the intended comparison order. Counts cover all signups; pages show added, removed and changed accounts. Signup timestamps are ignored. This compares stored signups, not current clan membership or historical roster settings.",
         {"before_report_id": {"type": "string", "maxLength": 32}, "after_report_id": {"type": "string", "maxLength": 32}, "offset": offset, "limit": limit},
         ("before_report_id", "after_report_id"), compare_roster_reports),
    )
    return tuple(RegisteredAgentTool(AgentToolDefinition(
        name=name, description=description,
        parameters={"type": "object", "properties": properties, "required": list(required), "additionalProperties": False},
    ), handler) for name, description, properties, required, handler in definitions)


async def find_rosters(context: AgentRequestContext, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    rosters = await context.roster_queries.list_for_guild(context.guild.id)
    query = str(arguments.get("query") or "").casefold()
    matches = []
    for roster in rosters:
        if roster.guild_id != context.guild.id:
            continue
        clan = CLANS.get(roster.clan_code)
        aliases = (str(roster.id), roster.name, roster.clan_code, clan.name if clan else "")
        if not query or any(query in value.casefold() for value in aliases):
            matches.append(roster_summary(roster))
    matches.sort(key=lambda item: (item["name"].casefold(), item["roster_id"]))
    offset = arguments.get("offset", 0)
    await require_evidence_access(context)
    return {"rosters": matches[offset:offset + 25], "matched_count": len(matches),
            "next_offset": offset + 25 if offset + 25 < len(matches) else None}


async def list_roster_cycles(context: AgentRequestContext, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    page = await context.roster_queries.cycles(
        context.guild.id, arguments["roster_id"], before_id=arguments.get("before_id"),
        limit=arguments.get("limit", 25),
    )
    await require_evidence_access(context)
    if page is None:
        return {"error": "That roster is not available in this server."}
    return {"roster_id": arguments["roster_id"], "cycles": [asdict(cycle) for cycle in page.cycles],
            "next_before_id": page.next_before_id, "order": "newest_created_first"}


async def read_roster(context: AgentRequestContext, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    try:
        snapshot = await context.roster_queries.snapshot(
            context.guild.id, arguments["roster_id"], cycle_id=arguments.get("cycle_id"),
        )
    except KeyError:
        return {"error": "That cycle does not belong to this roster or is no longer available."}
    if snapshot is None or snapshot.roster.guild_id != context.guild.id:
        return {"error": "That roster is not available in this server."}
    await require_evidence_access(context)
    report = RosterReport(uuid4().hex, snapshot)
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {"error": "The complete roster report is too large to retain in this conversation."}
    return await _report_page(context, report)


async def read_roster_report(context: AgentRequestContext, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if not isinstance(report, RosterReport) or report.snapshot.roster.guild_id != context.guild.id:
        return {"error": "That roster report is not available in this conversation. Read the roster again."}
    return await _report_page(context, report, offset=arguments.get("offset", 0), limit=arguments.get("limit", 25))


async def compare_roster_reports(context: AgentRequestContext, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    before = context.state.reports.get(arguments["before_report_id"])
    after = context.state.reports.get(arguments["after_report_id"])
    if any(not isinstance(report, RosterReport) or report.snapshot.roster.guild_id != context.guild.id for report in (before, after)):
        return {"error": "Both roster reports must be available in this conversation and server. Read the missing rosters again."}
    result = compare_reports(before, after, offset=arguments.get("offset", 0), limit=arguments.get("limit", 25))
    await require_evidence_access(context)
    return result


async def _report_page(context: AgentRequestContext, report: RosterReport, *, offset: int = 0, limit: int = 25) -> Mapping[str, Any]:
    result = report.page(offset=offset, limit=limit)
    # Core access covers stored bot data. Publication links additionally require
    # current requester/bot channel access; never expose private post locations.
    posts = []
    access = {}
    for post in report.snapshot.posts:
        if post.channel_id not in access:
            access[post.channel_id] = await accessible_message_channel(context, post.channel_id) is not None
        if access[post.channel_id]:
            posts.append({"channel_id": post.channel_id, "message_id": post.message_id,
                          "url": f"https://discord.com/channels/{context.guild.id}/{post.channel_id}/{post.message_id}"})
            context.state.source_channels.add(post.channel_id)
            if len(posts) == 25:
                break
    result["accessible_posts"] = posts
    result["post_list_may_be_incomplete"] = True
    await require_evidence_access(context)
    return result
