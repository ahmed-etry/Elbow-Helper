"""Compatibility registration for the shared people link view."""

from elbow_helper.infrastructure.ai import AgentToolDefinition
from ..engine.capability_contract import CapabilityContract
from ..models import RegisteredAgentTool
from .people import read_people_links


def account_read_tools():
    return (RegisteredAgentTool(
        AgentToolDefinition(
            "read_linked_accounts", "Resolve members to all of their bot-linked Clash accounts.",
            {"type": "object", "properties": {
                "member_ids": {"type": "array", "minItems": 1, "maxItems": 200,
                               "items": {"type": "integer", "minimum": 1}},
            }, "required": ["member_ids"], "additionalProperties": False},
            usage_note="Use before player lookups. These bot links are separate from ClashPerk links; no API token is needed to read a linked account's public profile.",
        ), read_people_links,
        contract=CapabilityContract(entity_fields=(("member_ids", "discord_member_set"),)),
        returns="accounts:[{member_id,player_tag,player_name}],unlinked_member_ids,member_ids,account_count,member_count",
    ),)
