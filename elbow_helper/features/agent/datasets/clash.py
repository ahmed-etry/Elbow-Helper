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
from ..research.history import search_date
from .battle_logs import select_battles
from .clash_cache import MAX_AGE_SECONDS
from .findings import AGGREGATES, FIELD_MEANINGS, battle_aggregate, cohort_aggregate


ENDPOINTS = {
    "clan": "/clans/{tag}",
    "current_war": "/clans/{tag}/currentwar",
    "cwl_group": "/clans/{tag}/currentwar/leaguegroup",
    "cwl_war": "/clanwarleagues/wars/{tag}",
    "war_log": "/clans/{tag}/warlog",
    "capital_raids": "/clans/{tag}/capitalraidseasons",
    "player": "/players/{tag}",
    "battle_log": "/players/{tag}/battlelog",
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
PLAYER_CLAN_FIELDS = ("tag", "name", "clanLevel")
LEAGUE_FIELDS = ("name",)
HERO_FIELDS = ("name", "level", "maxLevel", "village")
CLAN_MEMBER_FIELDS = tuple(
    "tag name role townHallLevel expLevel trophies donations donationsReceived clanRank".split()
)
CWL_GROUP_FIELDS = ("state", "season")
CWL_CLAN_FIELDS = ("tag", "name", "clanLevel")
CWL_MEMBER_FIELDS = ("tag", "name", "townHallLevel")
WAR_LOG_FIELDS = ("result", "endTime", "teamSize", "attacksPerMember")
WAR_SIDE_FIELDS = ("tag", "name", "stars", "destructionPercentage")
WAR_CLAN_FIELDS = (*WAR_SIDE_FIELDS, "attacks", "expEarned")
BATTLE_LOG_FIELDS = (
    "battleType", "battleTimestamp", "attack", "opponentPlayerTag", "opponentName",
    "stars", "destructionPercentage", "battleTime",
)


def _fields(value, names):
    return {name: value[name] for name in names if name in value}


def collection_counts(data, original, path=""):
    """Count retained collections against the uncut API response."""
    counts = {}
    if isinstance(data, list) and isinstance(original, list):
        counts[path] = {"returned_rows": len(data), "total_rows": len(original)}
        for index, (row, source) in enumerate(zip(data, original)):
            counts.update(collection_counts(row, source, f"{path}/{index}"))
    elif isinstance(data, dict) and isinstance(original, dict):
        for key, value in data.items():
            counts.update(collection_counts(value, original.get(key), f"{path}/{key}"))
    return counts


def summary(kind, payload):
    if kind in ("current_war", "cwl_war"):
        return payload
    if kind == "player":
        result = _fields(payload, PLAYER_FIELDS)
        for field, names in (("clan", PLAYER_CLAN_FIELDS), ("league", LEAGUE_FIELDS)):
            if isinstance(payload.get(field), dict):
                result[field] = _fields(payload[field], names)
        result["heroes"] = [
            _fields(row, HERO_FIELDS)
            for row in payload.get("heroes", [])
        ]
        return result
    if kind == "clan":
        result = _fields(payload, CLAN_FIELDS)
        if "description" in result:
            result["description"] = result["description"][:300]
        for field in ("warLeague", "capitalLeague"):
            if isinstance(payload.get(field), dict):
                result[field] = _fields(payload[field], LEAGUE_FIELDS)
        result["memberList"] = [
            _fields(row, CLAN_MEMBER_FIELDS)
            for row in payload.get("memberList", [])
        ]
        return result
    if kind == "cwl_group":
        result = _fields(payload, CWL_GROUP_FIELDS)
        result["clans"] = [
            {
                **_fields(clan, CWL_CLAN_FIELDS),
                "members": [
                    _fields(row, CWL_MEMBER_FIELDS)
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
                **_fields(row, WAR_LOG_FIELDS),
                **{
                    field: _fields(
                        row[field], WAR_CLAN_FIELDS if field == "clan" else WAR_SIDE_FIELDS,
                    )
                    for field in ("clan", "opponent") if field in row
                },
            }
            for row in payload.get("items", [])
        ]}
    if kind == "battle_log":
        return {"items": [_fields(row, BATTLE_LOG_FIELDS) for row in payload.get("items", [])]}
    return {"items": [
        {key: value for key, value in row.items() if key not in ("attackLog", "defenseLog")}
        for row in payload.get("items", [])
    ]}


async def read_clash(context, arguments):
    cache = getattr(context, "clash_reads", None)
    try:
        await require_evidence_access(context)
    except BaseException:
        if cache is not None:
            cache.clear()
        raise
    client = getattr(context.bot, "clash_client", None)
    if client is None or not client.configured:
        return {"error": "The Clash API is not set up."}
    if cache is not None:
        cache.bind((id(client), context.guild.id, context.member.id))
    max_age = arguments.get("max_age_seconds", MAX_AGE_SECONDS)
    if type(max_age) is not int or not 0 <= max_age <= MAX_AGE_SECONDS:
        return {"error": "Clash freshness must be between zero and thirty seconds."}
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
            if isinstance(value, str) and kind not in ("player", "battle_log", "cwl_war") else None
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
    order, offset = arguments.get("order", "newest_first"), arguments.get("offset", 0)
    try:
        after, before = search_date(arguments.get("after")), search_date(arguments.get("before"))
        if (order not in ("newest_first", "oldest_first") or type(offset) is not int or offset < 0
                or after is not None and before is not None and after >= before):
            raise ValueError("Invalid battle selection")
        if kind != "battle_log" and any(key in arguments for key in ("order", "offset", "after", "before")):
            raise ValueError("Battle selection applies only to battle logs")
    except (ValueError, TypeError, AttributeError):
        return {"error": "Use valid ISO dates, chronological order and offset for battle logs only."}
    aggregate = arguments.get("aggregate")
    if aggregate is not None and (kind != "battle_log" or aggregate not in AGGREGATES):
        return {"error": "Choose a supported battle-log aggregate."}
    # Repeated tags must not multiply a cohort's denominators.
    if aggregate is not None:
        normalized = list(dict.fromkeys(normalized))
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
                path += "?limit=" + ("10" if kind == "capital_raids" else "50")
            async def fetch():
                return await client.get(
                    path, attempts=1, timeout_seconds=max(0.1, deadline - time.monotonic()),
                )
            if cache is None:
                response = await fetch()
                observed_at, reused = datetime.now(timezone.utc).isoformat(), False
            else:
                response, observed_at, reused = await cache.get((kind, tag), fetch, max_age=max_age)
            if response.ok and isinstance(response.payload, dict):
                original = payload = response.payload
                coverage = None
                if kind == "battle_log":
                    # The battle-log route has no verified server-side limit parameter.
                    payload, coverage = select_battles(
                        payload, limit=limit, offset=offset, order=order, after=after, before=before,
                    )
                elif kind in ("war_log", "capital_raids"):
                    payload = {**payload, "items": payload.get("items", [])[:limit]}
                items[index] = {
                    "tag": tag, "status": "ok",
                    "observed_at": observed_at, "reused": reused,
                    "data": (
                        payload if arguments.get("detail") == "full"
                        else summary(kind, payload)
                    ),
                }
                counts = collection_counts(items[index]["data"], payload)
                if "items" in original:
                    counts["/items"] = {"returned_rows": len(payload.get("items", [])),
                                        "total_rows": len(original["items"])}
                items[index]["row_counts"] = counts or {"/": {"returned_rows": 1, "total_rows": 1}}
                items[index]["truncated"] = any(
                    count["returned_rows"] < count["total_rows"] for count in counts.values()
                )
                if coverage is not None:
                    items[index]["coverage"] = coverage
                if aggregate is not None:
                    items[index]["aggregate"] = battle_aggregate(
                        original, aggregate, after=after, before=before,
                    )
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
    try:
        await require_evidence_access(context)
    except BaseException:
        if cache is not None:
            cache.clear()
        raise
    truncated = any(item.get("truncated", False) for item in items)
    return {"source": "clash_api", "kind": kind, "items": items,
            "field_meanings": FIELD_MEANINGS.get(kind, {}),
            **({"aggregate": cohort_aggregate(items, aggregate, after=after, before=before)}
               if aggregate is not None else {}),
            "observed_at": min((item["observed_at"] for item in items if "observed_at" in item),
                               default=datetime.now(timezone.utc).isoformat()),
            "truncated": truncated,
            "flags": {"status": "partial" if kind == "battle_log" or truncated or any(
                item["status"] != "ok" for item in items) else "complete", "truncated": truncated,
                "limits": ["rolling_history_only"] if kind == "battle_log" else []}}


def clash_tools():
    return (RegisteredAgentTool(
        AgentToolDefinition(
            "read_clash",
            "Read current Clash clan, war, CWL, raid, player or battle log data.",
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
                    "order": {"type": "string", "enum": ["newest_first", "oldest_first"],
                              "default": "newest_first"},
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                    "after": {"type": "string", "maxLength": 40},
                    "before": {"type": "string", "maxLength": 40},
                    "aggregate": {"type": "string", "enum": list(AGGREGATES)},
                    "max_age_seconds": {"type": "integer", "minimum": 0, "maximum": 30,
                                        "default": 30},
                },
                "required": ["kind", "tags"], "additionalProperties": False,
            },
            usage_note="Max 200 players/20 others/5 full. Raids limit 1..10 default 2; others 1..50 default 10. Battle dates UTC; before exclusive. Aggregates before paging.",
        ),
        read_clash,
        contract=CapabilityContract(), returns="items[].tag,status,data; observed_at",
    ),)
