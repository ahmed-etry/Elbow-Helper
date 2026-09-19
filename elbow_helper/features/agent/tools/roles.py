"""Role discovery and complete member/account reports, without role changes."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.features.account_links.evidence import (
    member_account_evidence, refresh_account_locations,
)
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..reports.base import ArtifactCapacityError, retain_report
from ..access import require_evidence_access
from ..models import AgentRequestContext, RegisteredAgentTool
from ..reports.roles import RoleAccountReport, revise_role_account_report
from ..reports.role_comparisons import compare_role_account_reports as compare_reports


ROLE_PURPOSES = (
    "member_role_id", "war_role_id", "cwl_role_id", "leadership_role_id",
    "cwl_helper_role_id", "cwl_bench_role_id",
)
REPORT_ID_FIELD = {
    "type": "string",
    "maxLength": 32,
    "description": "Report ID returned by audit_role_accounts in this conversation.",
}


def role_tools() -> tuple[RegisteredAgentTool, ...]:
    return (
        RegisteredAgentTool(
            AgentToolDefinition(
                name="find_discord_roles",
                description=(
                    "Find server roles by name or ID and inspect their membership counts "
                    "and configured clan purpose. Distinguish similarly named roles before "
                    "selecting members for a report."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "maxLength": 100},
                        "offset": {"type": "integer", "minimum": 0},
                    },
                    "additionalProperties": False,
                },
            ),
            find_discord_roles,
        ),
        RegisteredAgentTool(
            AgentToolDefinition(
                name="audit_role_accounts",
                description=(
                    "Build an account report for every member holding any of the selected roles. "
                    "Include members without linked accounts. Distinguish refreshed locations, "
                    "last-known locations, and failed lookups. Role-removal recommendations "
                    "require criteria supplied by the asker or an approved policy."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "role_ids": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 1},
                            "minItems": 1,
                            "maxItems": 10,
                            "uniqueItems": True,
                        },
                        "refresh_locations": {"type": "boolean", "default": True},
                    },
                    "required": ["role_ids"],
                    "additionalProperties": False,
                },
            ),
            audit_role_accounts,
        ),
        RegisteredAgentTool(
            AgentToolDefinition(
                name="refresh_role_account_report",
                description=(
                    "Refresh up to 25 unresolved account locations from an exact "
                    "retained role/account report. Returns a new immutable report "
                    "revision, preserves prior evidence and never changes account "
                    "ownership or Discord roles. Omit player_tags to advance the "
                    "next not-yet-attempted unresolved batch."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "report_id": REPORT_ID_FIELD,
                        "player_tags": {
                            "type": "array", "minItems": 1, "maxItems": 25,
                            "uniqueItems": True,
                            "items": {"type": "string", "maxLength": 20},
                        },
                        "limit": {
                            "type": "integer", "minimum": 1, "maximum": 25,
                        },
                    },
                    "required": ["report_id"],
                    "additionalProperties": False,
                },
            ),
            refresh_role_account_report,
        ),
        RegisteredAgentTool(
            AgentToolDefinition(
                name="read_role_account_report",
                description=(
                    "Read or filter an existing report without repeating its account lookups. "
                    "outside_clan includes only members whose linked accounts were all refreshed "
                    "and none were in the specified clan. no_links and unverified are separate "
                    "unresolved groups. Use next_offset to read further pages."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "report_id": REPORT_ID_FIELD,
                        "selection": {
                            "type": "string",
                            "enum": ["all", "no_links", "unverified", "outside_clan"],
                        },
                        "clan_code": {"type": "string", "maxLength": 10},
                        "member_id": {"type": "integer", "minimum": 1},
                        "offset": {"type": "integer", "minimum": 0},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                    },
                    "required": ["report_id"],
                    "additionalProperties": False,
                },
            ),
            read_role_account_report,
        ),
        RegisteredAgentTool(
            AgentToolDefinition(
                name="compare_role_account_reports",
                description=(
                    "Compare two complete retained role/account reports in explicit "
                    "before/after order. Counts cover all selected roles, members and "
                    "linked accounts; pages separate ownership, link metadata, account "
                    "profile, observed-location and evidence-status changes. Observation timestamps are "
                    "ignored, and no cause or live state is inferred."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "before_report_id": REPORT_ID_FIELD,
                        "after_report_id": REPORT_ID_FIELD,
                        "offset": {"type": "integer", "minimum": 0},
                        "limit": {
                            "type": "integer", "minimum": 1, "maximum": 25,
                        },
                    },
                    "required": ["before_report_id", "after_report_id"],
                    "additionalProperties": False,
                },
            ),
            compare_role_account_reports,
        ),
    )


async def _complete_members(guild: Any) -> tuple[Any, ...]:
    if not guild.chunked:
        await guild.chunk(cache=True)
    if not guild.chunked:
        raise RuntimeError("Complete guild membership is unavailable")
    return tuple(guild.members)


async def find_discord_roles(context: AgentRequestContext, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    members = await _complete_members(context.guild)
    query = str(arguments.get("query") or "").strip().casefold()
    numeric = query.removeprefix("<@&").removesuffix(">")
    roles = []
    counts = Counter(role.id for member in members for role in member.roles)
    for role in context.guild.roles:
        purposes = [
            {"clan_code": clan.code, "purpose": field.removesuffix("_role_id")}
            for clan in CLANS.values()
            for field in ROLE_PURPOSES
            if getattr(clan, field) == role.id
        ]
        aliases = [value for entry in purposes for value in (entry["clan_code"], CLANS[entry["clan_code"]].name)]
        if (
            query and str(role.id) != numeric and query not in role.name.casefold()
            and not any(query in alias.casefold() for alias in aliases)
        ):
            continue
        roles.append({
            "role_id": role.id,
            "name": role.name,
            "position": role.position,
            "managed": role.managed,
            "permissions": [name for name, enabled in role.permissions if enabled],
            "member_count": counts[role.id],
            "clan_purposes": purposes,
        })
    offset = arguments.get("offset", 0)
    result = {
        "roles": roles[offset:offset + 25],
        "matched_count": len(roles),
        "next_offset": offset + 25 if offset + 25 < len(roles) else None,
    }
    await require_evidence_access(context)
    return result


async def audit_role_accounts(context: AgentRequestContext, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    role_ids = set(arguments["role_ids"])
    roles = [context.guild.get_role(value) for value in sorted(role_ids)]
    if any(role is None for role in roles):
        return {"error": "One or more selected roles do not exist in this server."}
    members = [
        member for member in await _complete_members(context.guild)
        if any(role.id in role_ids for role in member.roles)
    ]
    accounts = await member_account_evidence(
        context.account_links, (member.id for member in members),
        refresh=arguments.get("refresh_locations", True),
    )
    await require_evidence_access(context)
    report = RoleAccountReport(
        report_id=uuid4().hex,
        created_at=datetime.now(timezone.utc).isoformat(),
        roles=tuple({"role_id": role.id, "name": role.name} for role in roles),
        members=tuple(
            {
                "member_id": member.id,
                "member": member.display_name,
                "matched_roles": [role.name for role in member.roles if role.id in role_ids],
                "accounts": accounts.get(member.id, []),
            }
            for member in sorted(members, key=lambda item: (item.display_name.casefold(), item.id))
        ),
    )
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {"error": "The complete report is too large to retain in this conversation. Narrow the selected roles."}
    return report.page()


async def refresh_role_account_report(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    source = context.state.reports.get(arguments["report_id"])
    if not isinstance(source, RoleAccountReport):
        return {"error": "That report is not available in this conversation. Create a new report."}
    accounts = {
        account["player_tag"]: account
        for member in source.members for account in member["accounts"]
    }
    requested = arguments.get("player_tags")
    if requested is None:
        attempted = set(source.refresh_attempted_player_tags)
        candidates = [
            tag for tag, account in accounts.items()
            if account["location_status"] != "refreshed" and tag not in attempted
        ]
        selected_tags = tuple(candidates[:arguments.get("limit", 25)])
    else:
        normalized = tuple(normalize_player_tag(tag) for tag in requested)
        if any(tag is None for tag in normalized):
            return {"error": "Every requested account tag must be valid."}
        selected_tags = tuple(tag for tag in normalized if tag is not None)
        if len(selected_tags) != len(set(selected_tags)):
            return {"error": "Each account tag can be refreshed only once per batch."}
        missing = [tag for tag in selected_tags if tag not in accounts]
        if missing:
            return {"error": "Every requested account must belong to the retained report."}
        if any(accounts[tag]["location_status"] == "refreshed" for tag in selected_tags):
            return {"error": "Only unresolved account locations can be refreshed."}
    if not selected_tags:
        return {
            **source.manifest(),
            "refresh_batch_count": 0,
            "remaining_unattempted_accounts": 0,
            "note": "No unresolved account locations remain unattempted in this report.",
        }
    refreshed = await refresh_account_locations(
        context.account_links, tuple(accounts[tag] for tag in selected_tags),
    )
    await require_evidence_access(context)
    report = revise_role_account_report(
        source, refreshed, report_id=uuid4().hex,
    )
    try:
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {"error": "The refreshed report is too large to retain in this conversation."}
    result = report.page()
    result.update({
        "parent_report_id": source.report_id,
        "refresh_batch_count": len(selected_tags),
        "refresh_batch_player_tags": list(selected_tags),
        "remaining_unattempted_accounts": sum(
            account["location_status"] != "refreshed"
            and account["player_tag"] not in report.refresh_attempted_player_tags
            for member in report.members for account in member["accounts"]
        ),
    })
    return result


async def read_role_account_report(context: AgentRequestContext, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if not isinstance(report, RoleAccountReport):
        return {"error": "That report is not available in this conversation. Create a new report."}
    result = report.page(
        selection=arguments.get("selection", "all"),
        clan_code=str(arguments.get("clan_code") or "").upper(),
        member_id=arguments.get("member_id"),
        offset=arguments.get("offset", 0),
        limit=arguments.get("limit", 10),
    )
    await require_evidence_access(context)
    return result


async def compare_role_account_reports(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    before = context.state.reports.get(arguments["before_report_id"])
    after = context.state.reports.get(arguments["after_report_id"])
    if not isinstance(before, RoleAccountReport) or not isinstance(
        after, RoleAccountReport,
    ):
        return {
            "error": (
                "Both role/account reports must be available in this conversation. "
                "Create the missing reports again."
            )
        }
    result = compare_reports(
        before, after, offset=arguments.get("offset", 0),
        limit=arguments.get("limit", 25),
    )
    await require_evidence_access(context)
    return result
