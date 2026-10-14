"""Agent access to stored ranked observations, matching live Clash access."""

import asyncio
from datetime import datetime, timedelta, timezone
import sqlite3

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.ai import AgentToolDefinition
from ..access import require_evidence_access
from ..engine.capability_contract import CapabilityContract
from ..models import RegisteredAgentTool
from ..research.history import search_date


async def read_ranked_stats(context, arguments):
    await require_evidence_access(context)
    if context.ranked_queries is None:
        return {"error": "Ranked observations are not available."}
    selectors = [key for key in ("member_ids", "account_tags", "clan_tags") if arguments.get(key)]
    if len(selectors) != 1:
        return {"error": "Choose members, accounts or clans."}
    selector = selectors[0]
    values = arguments[selector]
    if not isinstance(values, list) or not 1 <= len(values) <= 200:
        return {"error": "Choose between one and two hundred members, accounts or clans."}
    if selector == "member_ids":
        if any(type(value) is not int or value <= 0 for value in values):
            return {"error": "Supply valid member IDs."}
    else:
        normalized = []
        for value in values:
            clan = CLANS.get(value.upper()) if isinstance(value, str) and selector == "clan_tags" else None
            tag = normalize_player_tag(clan.tag if clan else value)
            if tag is None:
                return {"error": "Use valid Clash tags or family clan codes."}
            normalized.append(tag)
        values = normalized
    try:
        after = search_date(arguments.get("after"))
        before = search_date(arguments.get("before")) or datetime.now(timezone.utc)
        if after is None or after >= before or (before - after) > timedelta(days=90):
            raise ValueError("Invalid window")
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 25)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 25:
            raise ValueError("Invalid page")
    except (ValueError, TypeError):
        return {"error": "Use ISO dates for a window of up to ninety days and a valid page."}
    try:
        result = await asyncio.to_thread(context.ranked_queries.window,
            after=after.isoformat(timespec="microseconds"), before=before.isoformat(timespec="microseconds"), selector=selector,
            values=tuple(dict.fromkeys(values)), offset=offset, limit=limit)
    except (sqlite3.Error, ValueError):
        return {"error": "Ranked observations could not be read."}
    await require_evidence_access(context)
    return result


def ranked_tools():
    return (RegisteredAgentTool(
        AgentToolDefinition(
            "read_ranked_stats", "Read ranked snapshots and observed wins for members, accounts or clans.",
            {"type": "object", "properties": {
                "member_ids": {"type": "array", "minItems": 1, "maxItems": 200,
                               "items": {"type": "integer", "minimum": 1}},
                "account_tags": {"type": "array", "minItems": 1, "maxItems": 200,
                                 "items": {"type": "string"}},
                "clan_tags": {"type": "array", "minItems": 1, "maxItems": 200,
                              "items": {"type": "string"}},
                "after": {"type": "string", "maxLength": 40},
                "before": {"type": "string", "maxLength": 40},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            }, "required": ["after"], "additionalProperties": False},
            usage_note="Choose one selector; max 90 days. Wins are not ranked attempts; unavailable attack counts/quotas stay null. Selection rank uses observed trophies.",
        ), read_ranked_stats,
        contract=CapabilityContract(entity_fields=(("member_ids", "discord_member_set"),
            ("account_tags", "clash_account_set"), ("clan_tags", "clan_set"))),
    ),)
