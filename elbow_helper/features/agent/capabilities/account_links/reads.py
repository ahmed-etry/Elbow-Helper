"""Discord-member and linked-account tools."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from typing import Mapping


from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ...engine.capability_contract import CapabilityContract
from ...access import lookup_level, ACCESS_RECRUITER_OR_CORE
from ...access import require_evidence_access
from ...models import AgentRequestContext
from ...models import RegisteredAgentTool
from ..validation import positive_int


MEMBER_RESULT_LIMIT = 8
MEMBER_DETAILS_LIMIT = 100
MEMBER_ID_LIMIT = 1000


def member_tools():
    return (
        RegisteredAgentTool(
            AgentToolDefinition(
                name="get_linked_accounts",
                description=(
                    "Get one member's current stored Clash account links. Return observed "
                    "family-clan locations separately from older last-seen link values, "
                    "with snapshot coverage and explicit truncation. Use for factual account questions, "
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
            contract=CapabilityContract(
                entity_fields=(("member_id", "discord_member"),),
                required_access=frozenset({ACCESS_RECRUITER_OR_CORE}),
            ),
        ),
        RegisteredAgentTool(
            AgentToolDefinition(
                name="get_account_link",
                description=(
                    "Check one exact Clash player tag against current stored Discord account "
                    "links. Return its primary flag and separately dated family-clan "
                    "observation when available. A current link does not establish who "
                    "owned the account historically."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "player_tag": {"type": "string", "minLength": 2, "maxLength": 20},
                    },
                    "required": ["player_tag"],
                    "additionalProperties": False,
                },
            ),
            get_account_link,
            contract=CapabilityContract(
                entity_fields=(("player_tag", "clash_account"),),
                required_access=frozenset({ACCESS_RECRUITER_OR_CORE}),
            ),
        ),
    )


@lookup_level(ACCESS_RECRUITER_OR_CORE)
async def get_linked_accounts(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    member_id = positive_int(arguments.get("member_id"))
    if member_id is None:
        return {"error": "A valid member_id is required."}
    await require_evidence_access(context)
    member = context.guild.get_member(member_id)
    rows = await asyncio.to_thread(
        context.account_links.get_links_for_user,
        member_id,
    )
    links_read_at = datetime.now(timezone.utc).isoformat()
    selected = rows[:25]
    tags = tuple(str(row.get("player_tag") or "") for row in selected)
    snapshot = context.account_links.get_player_locations_snapshot(tags)
    locations = snapshot.get("locations") or {}
    location_statuses = snapshot.get("location_statuses") or {}
    locations_complete = bool(snapshot.get("complete"))
    accounts = []
    for row in selected:
        player_tag = str(row.get("player_tag") or "")
        location = locations.get(player_tag) or {}
        location_status = (
            str(location_statuses.get(player_tag) or "observed_family_clan")
            if player_tag in locations else
            "not_in_complete_family_snapshot" if locations_complete else
            "location_unknown"
        )
        accounts.append(
            {
                "player_tag": player_tag,
                "linked_player_name": row.get("player_name_last_seen"),
                "primary": bool(row.get("is_primary")),
                "observed_player_name": location.get("player_name"),
                "observed_clan_code": location.get("clan_code"),
                "observed_clan_tag": location.get("clan_tag"),
                "observed_clan_role": location.get("role"),
                "last_seen_clan_code": row.get("last_seen_clan_code"),
                "last_seen_clan_tag": row.get("last_seen_clan_tag"),
                "last_seen_clan_role": row.get("last_seen_role"),
                "location_status": location_status,
            }
        )
    await require_evidence_access(context)
    return {
        "member_id": member_id,
        "member": member.display_name if member else None,
        "link_scope": "current_stored_links",
        "links_read_at": links_read_at,
        "location_snapshot_observed_at": snapshot.get("observed_at"),
        "location_snapshot_complete": locations_complete,
        "total_linked_accounts": len(rows),
        "accounts_truncated": len(rows) > len(selected),
        "accounts": accounts,
    }


@lookup_level(ACCESS_RECRUITER_OR_CORE)
async def get_account_link(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    player_tag = normalize_player_tag(arguments.get("player_tag"))
    if player_tag is None:
        return {"error": "That player tag is not valid."}
    await require_evidence_access(context)
    link = await asyncio.to_thread(context.account_links.get_link_by_tag, player_tag)
    links_read_at = datetime.now(timezone.utc).isoformat()
    snapshot = context.account_links.get_player_locations_snapshot((player_tag,))
    await require_evidence_access(context)
    location = (snapshot.get("locations") or {}).get(player_tag) or {}
    location_statuses = snapshot.get("location_statuses") or {}
    location_status = (
        str(location_statuses.get(player_tag) or "observed_family_clan")
        if location else
        "not_in_complete_family_snapshot" if snapshot.get("complete") else
        "location_unknown"
    )
    linked_member_id = positive_int(link.get("discord_user_id")) if link else None
    member = context.guild.get_member(linked_member_id) if linked_member_id else None
    return {
        "player_tag": player_tag,
        "link_status": "linked" if link else "unlinked",
        "links_read_at": links_read_at,
        "linked_member_id": linked_member_id,
        "linked_member_display_name": member.display_name if member else None,
        "linked_player_name": link.get("player_name_last_seen") if link else None,
        "primary": bool(link.get("is_primary")) if link else None,
        "last_seen_clan_code": link.get("last_seen_clan_code") if link else None,
        "last_seen_clan_tag": link.get("last_seen_clan_tag") if link else None,
        "last_seen_clan_role": link.get("last_seen_role") if link else None,
        "location_snapshot_observed_at": snapshot.get("observed_at"),
        "location_snapshot_complete": bool(snapshot.get("complete")),
        "location_status": location_status,
        "observed_player_name": location.get("player_name"),
        "observed_clan_code": location.get("clan_code"),
        "observed_clan_tag": location.get("clan_tag"),
        "observed_clan_role": location.get("role"),
    }
