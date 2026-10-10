"""Discord member and role discovery."""

from __future__ import annotations

from collections import Counter
from datetime import timezone
import re
from typing import Any, Mapping

import discord

from elbow_helper.configuration.clans import CLANS
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..engine.capability_contract import CapabilityContract
from ..access import require_evidence_access
from ..models import AgentRequestContext, RegisteredAgentTool
from ..capabilities.validation import positive_int
MEMBER_RESULT_LIMIT = 8
MEMBER_DETAILS_LIMIT = 1000
MEMBER_ID_LIMIT = 1000
ROLE_PURPOSES = (
    "member_role_id", "war_role_id", "cwl_role_id", "leadership_role_id", "cwl_helper_role_id",
    "cwl_bench_role_id",
)


async def find_discord_members(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    query = str(arguments.get("query") or "").strip()
    if not query:
        return {"error": "A member name, mention, or ID is required."}
    match = re.fullmatch(r"<@!?(\d+)>", query)
    numeric_id = int(match.group(1)) if match else (positive_int(query) or 0)
    needle = query.casefold()
    ranked: list[tuple[int, str, discord.Member]] = []
    for member in context.guild.members:
        names = {
            str(member.display_name or ""),
            str(member.name or ""),
            str(getattr(member, "global_name", "") or ""),
        }
        folded = {name.casefold() for name in names if name}
        if numeric_id and member.id == numeric_id:
            score = 0
        elif needle in folded:
            score = 1
        elif any(name.startswith(needle) for name in folded):
            score = 2
        elif any(needle in name for name in folded):
            score = 3
        else:
            continue
        ranked.append((score, member.display_name.casefold(), member))
    ranked.sort(key=lambda item: (item[0], item[1], item[2].id))
    return {
        "query": query,
        "members": [
            {
                "member_id": member.id,
                "display_name": member.display_name,
                "username": member.name,
                "joined_at": _joined_at(member),
                "mention": member.mention,
                "roles": [role.name for role in member.roles if not role.is_default()],
            }
            for _, _, member in ranked[:MEMBER_RESULT_LIMIT]
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
    if identifiers is None and role_ids is None:
        return {"error": "Choose member IDs or role IDs."}
    if role_ids is not None and (
        not isinstance(role_ids, list) or not 1 <= len(role_ids) <= 25
        or any(type(value) is not int or value <= 0 for value in role_ids)
    ):
        return {"error": "Choose between 1 and 25 role IDs."}
    if arguments.get("role_match", "any") not in ("any", "all"):
        return {"error": "Choose any or all roles."}
    selected_ids = set(identifiers) if isinstance(identifiers, list) else None
    if role_ids is not None:
        await require_evidence_access(context)
        members = await _complete_members(context.guild)
        requested = set(role_ids)
        identifiers = [
            member.id for member in members
            if (selected_ids is None or member.id in selected_ids)
            and (
                requested <= {role.id for role in member.roles}
                if arguments.get("role_match", "any") == "all"
                else requested.intersection(role.id for role in member.roles)
            )
        ]
        if not identifiers:
            return {
                "members": [], "total_members": 0, "missing_member_ids": [], "offset": 0,
                "limit": arguments.get("limit", 100), "next_offset": None,
            }
    if (not isinstance(identifiers, list)
            or role_ids is None and not 1 <= len(identifiers) <= MEMBER_ID_LIMIT
            or any(type(value) is not int or value <= 0 for value in identifiers)
            or len(set(identifiers)) != len(identifiers)):
        return {"error": "Provide between 1 and 1000 distinct member IDs."}
    offset, limit = arguments.get("offset", 0), arguments.get("limit", 100)
    if (type(offset) is not int or not 0 <= offset <= MEMBER_ID_LIMIT
            or type(limit) is not int or not 1 <= limit <= MEMBER_DETAILS_LIMIT):
        return {"error": "Use an offset from 0 to 1000 and a limit from 1 to 1000."}
    sorting = arguments.get("sort")
    if sorting is not None and sorting not in ("oldest_first", "newest_first"):
        return {"error": "Sort join dates oldest_first or newest_first."}
    await require_evidence_access(context)
    rows, missing = [], []
    for identifier in identifiers:
        member = context.guild.get_member(identifier)
        if member is None:
            missing.append(identifier)
            continue
        rows.append({
            "member_id": member.id, "display_name": member.display_name,
            "username": member.name, "joined_at": _joined_at(member),
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
        "members": selected, "total_members": len(rows), "missing_member_ids": missing,
        "offset": offset, "limit": limit,
        "next_offset": offset + len(selected) if offset + len(selected) < len(rows) else None,
    }


def member_tools():
    return (
        RegisteredAgentTool(
            AgentToolDefinition(
                name="find_discord_members",
                description=(
                    "Resolve a member name, mention, or Discord ID when the request does not "
                    "already identify the member unambiguously."
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
            ),
            find_discord_members,
            contract=CapabilityContract(entity_fields=()),
        ),
        RegisteredAgentTool(
            AgentToolDefinition(
                name="read_discord_members",
                description=(
                    "Read member details. Return display names, usernames, UTC server join times "
                    "and roles for up to 1000 exact member IDs, in pages of up to 1000. "
                    "Sort by joined_at oldest_first or newest_first before "
                    "paging; unknown join times come last. Omit sort to preserve the ID order."
                ),
                parameters={
                    "type": "object", "properties": {
                        "role_ids": {
                            "type": "array", "minItems": 1, "maxItems": 25,
                            "items": {"type": "integer", "minimum": 1},
                        },
                        "role_match": {"type": "string", "enum": ["any", "all"]},
                        "include_roles": {"type": "boolean"},
                        "member_ids": {"type": "array", "minItems": 1,
                                       "maxItems": MEMBER_ID_LIMIT, "uniqueItems": True,
                                       "items": {"type": "integer", "minimum": 1}},
                        "sort": {"type": "string", "enum": ["oldest_first", "newest_first"]},
                        "offset": {"type": "integer", "minimum": 0, "maximum": MEMBER_ID_LIMIT},
                        "limit": {"type": "integer", "minimum": 1, "maximum": MEMBER_DETAILS_LIMIT},
                    }, "required": [], "additionalProperties": False,
                },
            ),
            read_discord_members,
            contract=CapabilityContract(
                entity_fields=(("member_ids", "discord_member_set"),), filter_fields=("sort",),
            ),
        ),
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
        purposes = [
            {"clan_code": clan.code, "purpose": field.removesuffix("_role_id")}
            for clan in CLANS.values()
            for field in ROLE_PURPOSES
            if getattr(clan, field) == role.id
        ]
        aliases = [
            value for entry in purposes
            for value in (entry["clan_code"], CLANS[entry["clan_code"]].name)
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
