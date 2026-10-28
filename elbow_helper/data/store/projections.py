"""Validate source snapshots before any interval can establish absence."""

from collections import Counter
import json
import unicodedata

from elbow_helper.domain.player_tags import normalize_player_tag


CLAN_COUNTER_FIELDS = (
    "members", "clanPoints", "clanBuilderBasePoints", "clanCapitalPoints",
    "warWins", "warTies", "warLosses", "warWinStreak", "clanGold",
)
DONATION_FIELDS = (
    "donations", "donationsReceived", "trophies", "builderBaseTrophies",
    "clanRank", "previousClanRank",
)


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def search_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def profile_valid(payload: object, tag: str) -> bool:
    return (
        isinstance(payload, dict) and normalize_player_tag(payload.get("tag")) == tag
        and isinstance(payload.get("name"), str) and bool(payload["name"])
        and nonnegative_int(payload.get("members"))
        and all(nonnegative_int(payload[key]) for key in CLAN_COUNTER_FIELDS if key in payload)
    )


def roster_projection(payload: object, declared: int | None) -> tuple[list[dict], bool]:
    if not isinstance(payload, dict) or not isinstance(payload.get("memberList"), list):
        return [], False
    rows = []
    for raw in payload["memberList"]:
        if not isinstance(raw, dict):
            continue
        tag = normalize_player_tag(raw.get("tag"))
        if (tag is None or not isinstance(raw.get("name"), str) or not raw["name"]
                or raw.get("role") not in {"member", "admin", "coLeader", "leader"}
                or any(not nonnegative_int(raw[key]) for key in DONATION_FIELDS if key in raw)):
            continue
        rows.append({**raw, "tag": tag})
    counts = Counter(row["tag"] for row in rows)
    # Conflicting duplicate rows cannot supply trustworthy positive observations.
    unique = [row for row in rows if counts[row["tag"]] == 1]
    paging = payload.get("paging", {})
    no_more = (isinstance(paging, dict) and isinstance(paging.get("cursors", {}), dict)
               and not paging.get("cursors", {}).get("after"))
    complete = (declared is not None and len(unique) == len(payload["memberList"]) == declared and no_more)
    return unique, complete


def link_projection(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        raise ValueError("invalid_link_snapshot")
    rows = []
    seen = set()
    fields = ("player_name_last_seen", "last_seen_clan_tag", "last_seen_clan_code", "last_seen_role")
    for key, raw in payload.items():
        if not isinstance(raw, dict):
            raise ValueError("invalid_link_row")
        tag = normalize_player_tag(key)
        if (tag is None or normalize_player_tag(raw.get("player_tag")) != tag or tag in seen
                or type(raw.get("discord_user_id")) is not int or raw["discord_user_id"] <= 0
                or any(key not in raw or raw[key] is not None and not isinstance(raw[key], str) for key in fields)):
            raise ValueError("invalid_link_row")
        seen.add(tag)
        rows.append({**raw, "player_tag": tag})
    return rows


def discord_projection(rows: object) -> list[dict]:
    if not isinstance(rows, list):
        raise ValueError("invalid_member_snapshot")
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0
                or row["id"] in seen or not isinstance(row.get("username"), str)
                or not isinstance(row.get("display_name"), str)
                or row.get("nick") is not None and not isinstance(row["nick"], str)
                or type(row.get("bot")) is not bool or not isinstance(row.get("roles"), list)
                or any(type(role) is not int or role <= 0 for role in row["roles"])
                or len(set(row["roles"])) != len(row["roles"])
                or row.get("joined_at") is not None and not nonnegative_int(row["joined_at"])):
            raise ValueError("invalid_member_row")
        seen.add(row["id"])
    return rows
