"""Discord member and role discovery."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping

import discord

from elbow_helper.configuration.clans import CLANS
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..engine.capability_contract import CapabilityContract
from ..access import accessible_message_channel, require_evidence_access
from ..models import AgentRequestContext, RegisteredAgentTool
from ..member_names import duplicate_names, identity_fields
from ..capabilities.validation import invalid_arguments
from ..capabilities.hibernation.report import HibernationReport
from ..clan_roles import clan_role_purposes
from .member_identity import clarification, matching_members
MEMBER_RESULT_LIMIT = 8
MEMBER_DETAILS_LIMIT = 1000
MEMBER_ID_LIMIT = 1000


async def find_discord_members(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    query = str(arguments.get("query") or "").strip()
    if not query:
        return invalid_arguments("A member name, mention, or ID is required.", "query")
    members = await matching_members(context, query)
    duplicates = duplicate_names(getattr(context.guild, "members", members))
    if len(members) == 1 and (state := getattr(context, "state", None)) is not None:
        state.resolved_members[members[0].id] = query
    return {
        "query": query,
        "matched_count": len(members),
        "status": "resolved" if len(members) == 1 else "needs_input",
        **({"missing": [clarification(query, members)]} if len(members) != 1 else {}),
        "members": [
            {
                "member_id": member.id,
                **identity_fields(member, duplicates),
                "joined_at": _joined_at(member),
                "mention": member.mention,
                "roles": [role.name for role in member.roles if not role.is_default()],
            }
            for member in members[:MEMBER_RESULT_LIMIT]
        ],
    }


def _joined_at(member: discord.Member) -> str | None:
    joined = getattr(member, "joined_at", None)
    if joined is None:
        return None
    if joined.tzinfo is None:
        joined = joined.replace(tzinfo=timezone.utc)
    return joined.astimezone(timezone.utc).isoformat()


async def read_discord_members(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    identifiers = arguments.get("member_ids")
    role_ids = arguments.get("role_ids")
    report_id = arguments.get("hibernation_report_id")
    report = None
    if report_id is not None:
        if not isinstance(report_id, str) or not report_id or identifiers is not None or role_ids is not None:
            return invalid_arguments("Use a hibernation report instead of member or role IDs.", "hibernation_report_id")
        await require_evidence_access(context)
        report = context.state.reports.get(report_id)
        if not isinstance(report, HibernationReport) or report.guild_id != context.guild.id:
            return invalid_arguments("Read active hibernation to obtain a report for this conversation.", "hibernation_report_id")
        if await accessible_message_channel(context, report.source_channel_id) is None:
            return {"error": "Hibernation status evidence is not accessible from this conversation."}
        context.state.source_channels.add(report.source_channel_id)
        # Select from the complete retained snapshot, never the visible page or role count.
        identifiers = [row.member_id for row in report.snapshot.records]
    dates = {}
    for key in ("joined_after", "joined_before"):
        if key not in arguments:
            continue
        try:
            value = datetime.fromisoformat(arguments[key])
            if value.tzinfo is None:
                raise ValueError
            dates[key] = value.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            return invalid_arguments("Use timezone-aware Discord join dates.", key)
    if (len(dates) == 2 and dates["joined_after"] >= dates["joined_before"]):
        return invalid_arguments("The join window must end after it starts.", "joined_after", "joined_before")
    sorting = arguments.get("sort")
    if sorting is not None and sorting not in ("oldest_first", "newest_first"):
        return invalid_arguments("Sort join dates oldest_first or newest_first.", "sort")
    if identifiers is None and role_ids is None and not (sorting or dates):
        return invalid_arguments("Choose member IDs, role IDs or a join-date sort/window.", "member_ids", "role_ids", "sort")
    offset, limit = arguments.get("offset", 0), arguments.get("limit", 100)
    if (type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= MEMBER_DETAILS_LIMIT):
        return invalid_arguments("Use a non-negative offset and a limit from 1 to 1000.", "offset", "limit")
    if identifiers is not None and offset > MEMBER_ID_LIMIT:
        return invalid_arguments("The offset for explicit member IDs must be at most 1000.", "offset")
    if identifiers is not None and (
            not isinstance(identifiers, list) or not 1 <= len(identifiers) <= MEMBER_ID_LIMIT
            or any(type(value) is not int or value <= 0 for value in identifiers)
            or len(set(identifiers)) != len(identifiers)):
        return invalid_arguments("Provide between 1 and 1000 distinct member IDs.", "member_ids")
    if role_ids is not None and (
        not isinstance(role_ids, list) or not 1 <= len(role_ids) <= 25
        or any(type(value) is not int or value <= 0 for value in role_ids)
    ):
        return invalid_arguments("Choose between 1 and 25 role IDs.", "role_ids")
    if arguments.get("role_match", "any") not in ("any", "all"):
        return invalid_arguments("Choose any or all roles.", "role_match")
    selected_ids = set(identifiers) if isinstance(identifiers, list) else None
    whole_guild = identifiers is None and role_ids is None
    if role_ids is not None or whole_guild:
        await require_evidence_access(context)
        members = await _complete_members(context.guild)
        requested = set(role_ids or ())
        identifiers = [
            member.id for member in members
            if (selected_ids is None or member.id in selected_ids)
            and (whole_guild or (
                requested <= {role.id for role in member.roles}
                if arguments.get("role_match", "any") == "all"
                else requested.intersection(role.id for role in member.roles)
            ))
        ]
        if not identifiers:
            return {
                "members": [], "total_members": 0, "missing_member_ids": [], "offset": 0,
                "limit": arguments.get("limit", 100), "next_offset": None,
            }
    await require_evidence_access(context)
    if report is not None:
        await _complete_members(context.guild)
    duplicates = duplicate_names(getattr(context.guild, "members", ()))
    rows, missing, unknown_joins = [], [], []
    for identifier in identifiers:
        member = context.guild.get_member(identifier)
        if member is None:
            missing.append(identifier)
            continue
        joined = _joined_at(member)
        if joined is None:
            unknown_joins.append(identifier)
        if dates and (joined is None
                      or "joined_after" in dates and joined < dates["joined_after"]
                      or "joined_before" in dates and joined >= dates["joined_before"]):
            continue
        rows.append({
            "member_id": member.id, **identity_fields(member, duplicates), "joined_at": joined,
            "bot": bool(getattr(member, "bot", False)),
            **({"roles": [
                {"role_id": role.id, "name": role.name}
                for role in member.roles if not role.is_default()
            ]} if arguments.get("include_roles", False) else {}),
        })
    if sorting is not None:
        known = [row for row in rows if row["joined_at"] is not None]
        unknown = [row for row in rows if row["joined_at"] is None]
        known.sort(key=lambda row: row["member_id"])
        known.sort(key=lambda row: row["joined_at"], reverse=sorting == "newest_first")
        rows = known + sorted(unknown, key=lambda row: row["member_id"])
    selected = rows[offset:offset + limit]
    await require_evidence_access(context)
    return {
        "members": selected, "total_members": len(rows), "missing_member_ids": sorted(missing),
        "offset": offset, "limit": limit,
        "next_offset": offset + len(selected) if offset + len(selected) < len(rows) else None,
        **({"cohort": {
            "source": "active_hibernation_records", "report_id": report_id,
            "observed_at": report.snapshot.observed_at,
            "record_count": len(report.snapshot.records),
            "unknown_join_member_ids": sorted(unknown_joins),
        }} if report is not None else {}),
    }


def member_tools():
    return (
        RegisteredAgentTool(
            AgentToolDefinition(
                name="find_discord_members",
                description=(
                    "Find Discord members by all supplied names, nicknames, linked account names, mention or ID."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 100,
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                usage_note=(
                    "Match every name part, including nicknames. Ambiguous matches: "
                    "ask once or use explicit reply context. Never default to asker/owner."
                ),
            ),
            find_discord_members,
            contract=CapabilityContract(entity_fields=()),
        ),
        RegisteredAgentTool(
            AgentToolDefinition(
                name="read_discord_members",
                description=(
                    "Read server display names and join dates, including retained hibernation cohorts."
                ),
                parameters={
                    "type": "object", "properties": {
                        "role_ids": {
                            "type": "array", "minItems": 1, "maxItems": 25,
                            "items": {"type": "integer", "minimum": 1},
                        },
                        "role_match": {"type": "string", "enum": ["any", "all"]},
                        "include_roles": {"type": "boolean"},
                        "joined_after": {"type": "string", "format": "date-time"},
                        "joined_before": {"type": "string", "format": "date-time"},
                        "member_ids": {"type": "array", "minItems": 1,
                                       "maxItems": MEMBER_ID_LIMIT, "uniqueItems": True,
                                       "items": {"type": "integer", "minimum": 1}},
                        "hibernation_report_id": {"type": "string", "minLength": 1, "maxLength": 32,
                                                  "description": "Use the entire retained active hibernation cohort; excludes member_ids/role_ids."},
                        "sort": {"type": "string", "enum": ["oldest_first", "newest_first"]},
                        "offset": {"type": "integer", "minimum": 0},
                        "limit": {"type": "integer", "minimum": 1, "maximum": MEMBER_DETAILS_LIMIT},
                    }, "required": [], "additionalProperties": False,
                },
                usage_note=(
                    "hibernation_report_id selects the full workflow cohort before paging. "
                    "oldest_first sorts server joins; unknowns last, ties by ID. Roles are a different cohort."
                ),
            ),
            read_discord_members,
            contract=CapabilityContract(
                entity_fields=(("member_ids", "discord_member_set"), ("hibernation_report_id", "hibernation_report")),
                filter_fields=("sort", "joined_after", "joined_before"),
                retained_fields=("hibernation_report_id",),
            ),
        ),
        RegisteredAgentTool(
            AgentToolDefinition(
                name="find_discord_roles",
                description=(
                    "Check named guild roles, their holders and clan purpose before claiming a role is absent."
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
            contract=CapabilityContract(entity_fields=()),
        ),
    )


async def _complete_members(guild: Any) -> tuple[Any, ...]:
    if not guild.chunked:
        await guild.chunk(cache=True)
    if not guild.chunked:
        raise RuntimeError("Complete guild membership is unavailable")
    return tuple(guild.members)


async def find_discord_roles(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    members = await _complete_members(context.guild)
    query = str(arguments.get("query") or "").strip().casefold()
    numeric = query.removeprefix("<@&").removesuffix(">")
    roles = []
    counts = Counter(role.id for member in members for role in member.roles)
    for role in context.guild.roles:
        purposes = clan_role_purposes(role.id)
        aliases = [
            value for entry in purposes
            for value in (entry["clan_code"], CLANS[entry["clan_code"]].name, entry["purpose"])
        ]
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
            "members": [{"member_id": member.id, "display_name": member.display_name}
                        for member in members if any(held.id == role.id for held in member.roles)],
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
