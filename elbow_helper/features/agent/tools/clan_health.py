"""Read-only access to stored clan-health meaning and evidence."""

from __future__ import annotations

import asyncio
from typing import Any
from typing import Mapping

from elbow_helper.features.clan_health.evidence import load_player_health, load_clan_health
from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..models import AgentRequestContext
from ..models import RegisteredAgentTool
from .shared import bounded_int


HEALTH_PLAYER_RESULT_LIMIT = 10
HEALTH_WINDOW_DEFAULT_DAYS = 30
HEALTH_WINDOW_MAX_DAYS = 365


def clan_health_tools() -> tuple[RegisteredAgentTool, ...]:
    return (
        RegisteredAgentTool(
            AgentToolDefinition(
                name="find_clan_health_players",
                description=(
                    "Find stored clan-health players by player name, tag, or clan code before "
                    "requesting detailed health evidence."
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
            find_clan_health_players,
        ),
        RegisteredAgentTool(
            AgentToolDefinition(
                name="get_player_health",
                description=(
                    "Get stored activity, war, raid, progression, movement, and report evidence "
                    "for one exact Clash player tag over a bounded recent window."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "player_tag": {
                            "type": "string",
                            "description": "Exact Clash player tag returned by another tool.",
                        },
                        "days": {
                            "type": "integer",
                            "minimum": 7,
                            "maximum": HEALTH_WINDOW_MAX_DAYS,
                            "default": HEALTH_WINDOW_DEFAULT_DAYS,
                        },
                    },
                    "required": ["player_tag"],
                    "additionalProperties": False,
                },
            ),
            get_player_health,
        ),
        RegisteredAgentTool(
            AgentToolDefinition(
                name="get_clan_health",
                description=(
                    "Get the latest completed stored clan-health report for one Brown Elbow "
                    "family clan, including player statuses and evidence summaries."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "clan_code": {
                            "type": "string",
                            "description": "Brown Elbow clan code such as BEH or BE4.",
                        }
                    },
                    "required": ["clan_code"],
                    "additionalProperties": False,
                },
            ),
            get_clan_health,
        ),
    )


async def find_clan_health_players(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    query = str(arguments.get("query") or "").strip()
    if not query:
        return {"error": "A player name, tag, or clan code is required."}
    rows = await asyncio.to_thread(
        context.clan_health.repository.search_players,
        query,
        HEALTH_PLAYER_RESULT_LIMIT,
    )
    return {"query": query, "players": rows}


async def get_player_health(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    player_tag = normalize_player_tag(str(arguments.get("player_tag") or ""))
    if not player_tag:
        return {"error": "A valid Clash player tag is required."}
    days = bounded_int(
        arguments.get("days"),
        default=HEALTH_WINDOW_DEFAULT_DAYS,
        minimum=7,
        maximum=HEALTH_WINDOW_MAX_DAYS,
    )
    return await asyncio.to_thread(
        load_player_health,
        context.clan_health.repository,
        player_tag,
        days,
    )


async def get_clan_health(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    clan_code = str(arguments.get("clan_code") or "").strip().upper()
    if clan_code not in CLANS:
        return {
            "error": "Unknown Brown Elbow clan code.",
            "known_clan_codes": sorted(CLANS),
        }
    return await asyncio.to_thread(
        load_clan_health,
        context.clan_health.repository,
        clan_code,
    )
