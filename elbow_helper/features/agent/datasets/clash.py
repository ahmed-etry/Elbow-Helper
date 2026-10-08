"""Live Clash reads through the application-owned transport."""

import asyncio
from datetime import datetime, timezone
import time

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag, encode_clash_tag
from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..engine import budgets
from ..engine.capability_contract import CapabilityContract
from ..models import RegisteredAgentTool


ENDPOINTS = {
    "clan": "/clans/{tag}",
    "current_war": "/clans/{tag}/currentwar",
    "cwl_group": "/clans/{tag}/currentwar/leaguegroup",
    "cwl_war": "/clanwarleagues/wars/{tag}",
    "war_log": "/clans/{tag}/warlog",
    "capital_raids": "/clans/{tag}/capitalraidseasons",
    "player": "/players/{tag}",
}
PLAYER_FIELDS = (
    "tag name townHallLevel townHallWeaponLevel expLevel trophies bestTrophies warStars "
    "attackWins defenseWins builderHallLevel role warPreference donations donationsReceived "
    "clanCapitalContributions"
).split()
CLAN_FIELDS = (
    "tag name type description clanLevel clanPoints members warWins warLosses warTies "
    "warWinStreak isWarLogPublic requiredTownhallLevel"
).split()


def _fields(value, names):
    return {name: value[name] for name in names if name in value}


def summary(kind, payload):
    if kind in ("current_war", "cwl_war"):
        return payload
    if kind == "player":
        result = _fields(payload, PLAYER_FIELDS)
        for field, names in (("clan", ["tag", "name", "clanLevel"]), ("league", ["name"])):
            if isinstance(payload.get(field), dict):
                result[field] = _fields(payload[field], names)
        result["heroes"] = [
            _fields(row, ["name", "level", "maxLevel", "village"])
            for row in payload.get("heroes", [])
        ]
        return result
    if kind == "clan":
        result = _fields(payload, CLAN_FIELDS)
        if "description" in result:
            result["description"] = result["description"][:300]
        for field in ("warLeague", "capitalLeague"):
            if isinstance(payload.get(field), dict):
                result[field] = _fields(payload[field], ["name"])
        result["memberList"] = [
            _fields(row, (
                "tag name role townHallLevel expLevel trophies donations donationsReceived clanRank"
            ).split())
            for row in payload.get("memberList", [])
        ]
        return result
    if kind == "cwl_group":
        result = _fields(payload, ["state", "season"])
        result["clans"] = [
            {
                **_fields(clan, ["tag", "name", "clanLevel"]),
                "members": [
                    _fields(row, ["tag", "name", "townHallLevel"])
                    for row in clan.get("members", [])
                ],
            }
            for clan in payload.get("clans", [])
        ]
        result["rounds"] = [_fields(row, ["warTags"]) for row in payload.get("rounds", [])]
        return result
    if kind == "war_log":
        return {"items": [
            {
                **_fields(row, ["result", "endTime", "teamSize", "attacksPerMember"]),
                **{
                    field: _fields(row[field], [
                        "tag", "name", "stars", "destructionPercentage",
                        *(["attacks", "expEarned"] if field == "clan" else []),
                    ])
                    for field in ("clan", "opponent") if field in row
                },
            }
            for row in payload.get("items", [])
        ]}
    return {"items": [
        {key: value for key, value in row.items() if key not in ("attackLog", "defenseLog")}
        for row in payload.get("items", [])
    ]}


async def read_clash(context, arguments):
    await require_evidence_access(context)
    client = getattr(context.bot, "clash_client", None)
    if client is None or not client.configured:
        return {"error": "The Clash API is not set up."}
    kind, tags = arguments["kind"], arguments["tags"]
    cap = 200 if kind == "player" else 20
    if (
        kind not in ENDPOINTS or not 1 <= len(tags) <= cap
        or arguments.get("detail") == "full" and len(tags) > 5
    ):
        return {"error": "The selected Clash batch is too large or has an unknown kind."}
    normalized = []
    for value in tags:
        clan = (
            CLANS.get(value.upper())
            if isinstance(value, str) and kind not in ("player", "cwl_war") else None
        )
        tag = normalize_player_tag(clan.tag if clan else value)
        if tag is None and kind == "cwl_war" and value == "#0":
            tag = "#0"
        if tag is None:
            return {"error": "Use valid Clash tags or family clan codes."}
        normalized.append(tag)
    limit = arguments.get("limit", 2 if kind == "capital_raids" else 10)
    if type(limit) is not int or not 1 <= limit <= (10 if kind == "capital_raids" else 50):
        return {"error": "The selected Clash history limit is invalid."}
    items = [{"tag": tag, "status": "unavailable"} for tag in normalized]
    semaphore = asyncio.Semaphore(8)
    deadline = time.monotonic() + budgets.TOOL_TIMEOUT_SECONDS - 1
    if context.deadline_monotonic is not None:
        deadline = min(deadline, context.deadline_monotonic - 1)

    async def read(index, tag):
        if tag == "#0":
            items[index] = {"tag": tag, "status": "not_found"}
            return
        async with semaphore:
            path = ENDPOINTS[kind].format(tag=encode_clash_tag(tag))
            if kind in ("war_log", "capital_raids"):
                path += f"?limit={limit}"
            response = await client.get(
                path, attempts=1, timeout_seconds=max(0.1, deadline - time.monotonic()),
            )
            if response.ok and isinstance(response.payload, dict):
                items[index] = {
                    "tag": tag, "status": "ok",
                    "data": (
                        response.payload if arguments.get("detail") == "full"
                        else summary(kind, response.payload)
                    ),
                }
            else:
                status = (
                    "not_found" if response.status == 404
                    else "maintenance" if response.status == 503
                    else "private" if response.status == 403
                    and kind in ("war_log", "current_war", "cwl_group") else "unavailable"
                )
                items[index] = {"tag": tag, "status": status}
                if response.status is not None:
                    items[index]["http_status"] = response.status

    tasks = [asyncio.create_task(read(index, tag)) for index, tag in enumerate(normalized)]
    try:
        await asyncio.wait(tasks, timeout=max(0, deadline - time.monotonic()))
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    await require_evidence_access(context)
    return {"items": items, "observed_at": datetime.now(timezone.utc).isoformat()}


def clash_tools():
    return (RegisteredAgentTool(
        AgentToolDefinition(
            "read_clash",
            "Read current clans, wars, CWL, raids and player profiles from Clash.",
            {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(ENDPOINTS)},
                    "tags": {
                        "type": "array", "minItems": 1, "maxItems": 200,
                        "items": {"type": "string"},
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    "detail": {"type": "string", "enum": ["summary", "full"]},
                },
                "required": ["kind", "tags"], "additionalProperties": False,
            },
        ),
        read_clash,
        contract=CapabilityContract(), returns="items[].tag,status,data; observed_at",
    ),)
