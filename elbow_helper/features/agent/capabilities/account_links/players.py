"""Bounded live player profiles through the account-link service's shared client."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping

from elbow_helper.domain.player_tags import encode_clash_tag, normalize_player_tag
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ...access import require_evidence_access
from ...engine import budgets
from ...engine.capability_contract import CapabilityContract
from ...models import AgentRequestContext, RegisteredAgentTool


PLAYER_LIMIT = 200
PLAYER_CONCURRENCY = 8


def player_tools() -> tuple[RegisteredAgentTool, ...]:
    return (RegisteredAgentTool(
        AgentToolDefinition(
            name="read_live_players",
            description=("Live Clash player profiles with Town Hall counts. "
                         "Returns current names, Town Hall levels, clans and observation times, "
                         "with optional inclusive Town Hall filters. "
                         "Reports missing and unavailable tags separately."),
            parameters={
                "type": "object", "properties": {
                    "player_tags": {"type": "array", "minItems": 1, "maxItems": PLAYER_LIMIT,
                                    "items": {"type": "string", "maxLength": 20}},
                    "min_townhall": {"type": "integer", "minimum": 1},
                    "max_townhall": {"type": "integer", "minimum": 1},
                }, "required": ["player_tags"], "additionalProperties": False,
            },
        ), read_live_players,
        contract=CapabilityContract(
            entity_fields=(("player_tags", "clash_account_set"),),
            filter_fields=("min_townhall", "max_townhall"),
        ),
    ),)


async def read_live_players(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    values = arguments.get("player_tags")
    if not isinstance(values, list) or not 1 <= len(values) <= PLAYER_LIMIT:
        raise ValueError("Invalid live player batch size")
    normalized = [normalize_player_tag(value) if isinstance(value, str) else None
                  for value in values]
    if any(tag is None for tag in normalized):
        raise ValueError("Invalid live player tag")
    tags = tuple(dict.fromkeys(normalized))
    lower, upper = arguments.get("min_townhall"), arguments.get("max_townhall")
    if any(value is not None and (type(value) is not int or value < 1)
           for value in (lower, upper)) or lower is not None and upper is not None and lower > upper:
        raise ValueError("Invalid Town Hall filter")

    client = context.account_links.clash_client
    found: dict[str, dict[str, Any]] = {}
    missing: set[str] = set()
    pending = iter(tags)

    async def worker() -> None:
        for tag in pending:
            response = await client.get(f"/players/{encode_clash_tag(tag)}")
            if response.status == 404:
                missing.add(tag)
                continue
            payload = response.payload_object
            if not response.ok or not payload or normalize_player_tag(payload.get("tag")) != tag:
                continue
            name, townhall = payload.get("name"), payload.get("townHallLevel")
            if not isinstance(name, str) or not name or type(townhall) is not int or townhall < 1:
                continue
            clan = payload.get("clan")
            if clan is not None:
                if (not isinstance(clan, dict) or normalize_player_tag(clan.get("tag")) is None
                        or not isinstance(clan.get("name"), str) or not clan["name"]):
                    continue
                clan = {"tag": normalize_player_tag(clan["tag"]), "name": clan["name"]}
            found[tag] = {"player_tag": tag, "name": name, "townhall": townhall,
                          "current_clan": clan, "observed_at": datetime.now(timezone.utc).isoformat()}

    tasks = [asyncio.create_task(worker()) for _ in range(min(PLAYER_CONCURRENCY, len(tags)))]
    try:
        # Return completed profiles and explicit unresolved tags before the tool's outer timeout.
        async with asyncio.timeout(budgets.TOOL_TIMEOUT_SECONDS - 1.0):
            await asyncio.gather(*tasks)
    except TimeoutError:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    await require_evidence_access(context)
    accounts = [found[tag] for tag in tags if tag in found
                and (lower is None or found[tag]["townhall"] >= lower)
                and (upper is None or found[tag]["townhall"] <= upper)]
    counts = Counter(row["townhall"] for row in accounts)
    return {
        "accounts": accounts,
        "summary": {"requested_accounts": len(tags), "found_accounts": len(found),
                    "matching_accounts": len(accounts),
                    "counts_by_townhall": {str(level): counts[level] for level in sorted(counts)}},
        "not_found_tags": [tag for tag in tags if tag in missing],
        "unavailable_tags": [tag for tag in tags if tag not in found and tag not in missing],
    }
