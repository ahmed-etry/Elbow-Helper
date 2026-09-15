"""Discord-member and linked-account tools."""

from __future__ import annotations

import asyncio
import re
from typing import Any
from typing import Mapping

import discord

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..models import AgentRequestContext
from ..models import RegisteredAgentTool
from .shared import positive_int


MEMBER_RESULT_LIMIT = 8


def member_tools() -> tuple[RegisteredAgentTool, ...]:
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
        ),
        RegisteredAgentTool(
            AgentToolDefinition(
                name="get_linked_accounts",
                description=(
                    "Get Brown Elbow's stored Clash account links and latest known clan "
                    "locations for one Discord member. Use for factual account questions, "
                    "not merely to personalize banter."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "member_id": {"type": "integer", "minimum": 1}
                    },
                    "required": ["member_id"],
                    "additionalProperties": False,
                },
            ),
            get_linked_accounts,
        ),
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
                "mention": member.mention,
                "roles": [role.name for role in member.roles if not role.is_default()],
            }
            for _, _, member in ranked[:MEMBER_RESULT_LIMIT]
        ],
    }


async def get_linked_accounts(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    member_id = positive_int(arguments.get("member_id"))
    if member_id is None:
        return {"error": "A valid member_id is required."}
    member = context.guild.get_member(member_id)
    rows = await asyncio.to_thread(
        context.account_links.get_links_for_user,
        member_id,
    )
    accounts = []
    for row in rows[:25]:
        player_tag = str(row.get("player_tag") or "")
        location = context.account_links.get_player_location(player_tag) or {}
        accounts.append(
            {
                "player_tag": player_tag,
                "player_name": row.get("player_name_last_seen") or location.get("player_name"),
                "primary": bool(row.get("is_primary")),
                "clan_code": location.get("clan_code") or row.get("last_seen_clan_code"),
                "clan_tag": location.get("clan_tag") or row.get("last_seen_clan_tag"),
                "clan_role": location.get("role") or row.get("last_seen_role"),
            }
        )
    return {
        "member_id": member_id,
        "member": member.display_name if member else None,
        "accounts": accounts,
    }
