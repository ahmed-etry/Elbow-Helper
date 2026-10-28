"""Bounded account resolution using the bot's account-link snapshot."""

import asyncio

from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ...access import require_evidence_access
from ...engine.capability_contract import CapabilityContract
from ...models import RegisteredAgentTool


async def read_linked_accounts(context, arguments, *, maximum=200):
    await require_evidence_access(context)
    members = arguments.get("member_ids")
    if (not isinstance(members, list) or not 1 <= len(members) <= maximum
            or any(type(value) is not int or value <= 0 for value in members)):
        return {"error": "Choose between 1 and 200 member IDs."}
    if context.account_links is None:
        return {"error": "Account links are not available.", "cause": "missing_setup"}
    members = list(dict.fromkeys(members))
    links = await asyncio.to_thread(context.account_links.get_links_for_members, members)
    await require_evidence_access(context)
    rows = []
    for member_id in members:
        for link in links.get(member_id, ()):
            tag = normalize_player_tag(link.get("player_tag"))
            if tag is not None:
                rows.append({"member_id": member_id, "player_tag": tag,
                             "player_name": link.get("player_name_last_seen")})
    return {"source": "bot_account_links", "accounts": rows,
            "member_ids": members, "unlinked_member_ids": [value for value in members
                if not any(row["member_id"] == value for row in rows)],
            "account_count": len(rows), "member_count": len(members),
            "field_meanings": {"member_id": "Discord member owning the linked account",
                               "player_tag": "one Clash account; a member may own several",
                               "player_name": "last recorded name, not a current profile"}}


def account_read_tools():
    return (RegisteredAgentTool(
        AgentToolDefinition(
            "read_linked_accounts", "Resolve members to all of their bot-linked Clash accounts.",
            {"type": "object", "properties": {
                "member_ids": {"type": "array", "minItems": 1, "maxItems": 200,
                               "items": {"type": "integer", "minimum": 1}},
            }, "required": ["member_ids"], "additionalProperties": False},
            usage_note="Use before player lookups. These bot links are separate from ClashPerk links; no API token is needed to read a linked account's public profile.",
        ), read_linked_accounts,
        contract=CapabilityContract(entity_fields=(("member_ids", "discord_member_set"),)),
        returns="accounts:[{member_id,player_tag,player_name}],unlinked_member_ids,member_ids,account_count,member_count",
    ),)
