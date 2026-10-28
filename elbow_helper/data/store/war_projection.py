"""Typed API projections, including explicit partial coverage and attack stubs."""

from datetime import datetime, timezone
import math

from elbow_helper.domain.player_tags import normalize_player_tag
from .projections import canonical_json, nonnegative_int


CWL_ROUNDS = 7
CWL_ROSTER_SIZE = 50


def api_time(value):
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.strptime(value, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc).timestamp() * 1_000_000)
    except ValueError:
        return None


def number(value, maximum=None):
    return (type(value) in (int, float) and math.isfinite(value) and value >= 0
            and (maximum is None or value <= maximum))


def optional_int(raw, key):
    value = raw.get(key)
    if value is not None and not nonnegative_int(value):
        raise ValueError("invalid_count")
    return value


def text(raw, key):
    value = raw.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError("invalid_text")
    return value


def badge(raw):
    value = raw.get("badgeUrls")
    if value is not None and (not isinstance(value, dict)
                              or any(not isinstance(url, str) for key, url in value.items()
                                     if key in {"small", "medium", "large"})):
        raise ValueError("invalid_badge")
    return canonical_json(value) if value is not None else None


def war_type(raw, cwl=False):
    if cwl:
        return "CWL"
    explicit = {"regular": "REG", "REG": "REG", "friendly": "FRIENDLY", "FRIENDLY": "FRIENDLY",
                "cwl": "CWL", "CWL": "CWL"}.get(raw.get("type"), "UNKNOWN")
    if type(raw.get("isFriendly")) is bool:
        friendly = "FRIENDLY" if raw["isFriendly"] else "REG"
        return friendly if explicit in {"UNKNOWN", friendly} else "UNKNOWN"
    return explicit


def side_projection(raw, *, destruction_maximum=100):
    if not isinstance(raw, dict) or not (tag := normalize_player_tag(raw.get("tag"))):
        raise ValueError("invalid_side")
    destruction = raw.get("destructionPercentage")
    if destruction is not None and not number(destruction, destruction_maximum):
        raise ValueError("invalid_destruction")
    return {"clan_tag": tag, "name": text(raw, "name"), "level": optional_int(raw, "clanLevel"),
            "badge_json": badge(raw), "stars": optional_int(raw, "stars"), "destruction": destruction,
            "attacks": optional_int(raw, "attacks"), "xp_earned": optional_int(raw, "expEarned"),
            "result": None, "extra_json": canonical_json({"field_presence": sorted(set(raw) & {
                "tag", "name", "clanLevel", "badgeUrls", "stars", "destructionPercentage", "attacks", "expEarned"})})}


def attack_projection(raw, stub=False):
    if not isinstance(raw, dict):
        raise ValueError("invalid_attack")
    attacker = normalize_player_tag(raw.get("attackerTag"))
    defender = normalize_player_tag(raw.get("defenderTag"))
    order = raw.get("order")
    if not attacker or not defender or not nonnegative_int(order) or order < 1:
        raise ValueError("invalid_attack_identity")
    stars, destruction, duration = raw.get("stars"), raw.get("destructionPercentage"), raw.get("duration")
    if (stars is not None and (not nonnegative_int(stars) or stars > 3)
            or destruction is not None and not number(destruction, 100)
            or duration is not None and not nonnegative_int(duration)):
        raise ValueError("invalid_attack_values")
    extra = {"stub": stub, "field_presence": sorted(raw.keys() & {
        "attackerTag", "defenderTag", "stars", "destructionPercentage", "order", "duration"})}
    known = {"attackerTag", "defenderTag", "stars", "destructionPercentage", "order", "duration"}
    complete = not stub and known <= raw.keys()
    return {"attack_order": order, "attacker": attacker, "defender": defender,
            "stars": stars, "destruction": destruction, "duration": duration,
            "extra_json": canonical_json(extra)}, complete


def project_war(raw, *, war_tag=None, aggregate=False):
    if not isinstance(raw, dict):
        raise ValueError("invalid_war")
    state = "warEnded" if aggregate else raw.get("state")
    if state not in {"preparation", "inWar", "warEnded"}:
        raise ValueError("invalid_state")
    end = api_time(raw.get("endTime"))
    preparation, start = api_time(raw.get("preparationStartTime")), api_time(raw.get("startTime"))
    war_start = api_time(raw.get("warStartTime"))
    if end is None or not aggregate and (preparation is None or start is None or not preparation <= start <= end):
        raise ValueError("invalid_war_time")
    if "warStartTime" in raw and (war_start is None or not aggregate and not preparation <= war_start <= end):
        raise ValueError("invalid_war_time")
    size, per_member = optional_int(raw, "teamSize"), optional_int(raw, "attacksPerMember")
    if size is not None and size < 1:
        raise ValueError("invalid_team_size")
    sides = sorted([(side_projection(raw.get(key)), raw.get(key)) for key in ("clan", "opponent")],
                   key=lambda pair: pair[0]["clan_tag"])
    if sides[0][0]["clan_tag"] == sides[1][0]["clan_tag"]:
        raise ValueError("duplicate_side")
    result = raw.get("result") if aggregate else None
    if result in {"win", "lose", "tie"}:
        for projected, source in sides:
            projected["result"] = result if source is raw["clan"] else {"win": "lose", "lose": "win", "tie": "tie"}[result]
    members, attacks, stubs, collisions = [], {}, [], set()
    roster_complete, attacks_complete = not aggregate and size is not None, not aggregate
    for side, (projected, source) in enumerate(sides):
        roster = source.get("members")
        if not isinstance(roster, list):
            roster_complete = attacks_complete = False
            continue
        seen, positions, used = set(), set(), 0
        for member in roster:
            try:
                if not isinstance(member, dict) or not (tag := normalize_player_tag(member.get("tag"))) or tag in seen:
                    raise ValueError("invalid_member")
                name, townhall, position = text(member, "name"), optional_int(member, "townhallLevel"), optional_int(member, "mapPosition")
                # CWL keeps each player's position in the season squad, even
                # when only a smaller, non-contiguous lineup fights this round.
                position_limit = CWL_ROSTER_SIZE if war_tag else size
                if (position is None or position < 1 or position_limit is not None and position > position_limit
                        or position in positions or name is None or townhall is None):
                    roster_complete = False
                seen.add(tag)
                positions.add(position)
                outgoing = member.get("attacks", [])
                if not isinstance(outgoing, list):
                    outgoing = []
                    attacks_complete = False
                for attack in outgoing:
                    try:
                        projected_attack, complete = attack_projection(attack)
                        if projected_attack["attacker"] != tag:
                            raise ValueError("wrong_attacker")
                        order = projected_attack["attack_order"]
                        if order in attacks:
                            collisions.add(order)
                        else:
                            attacks[order] = projected_attack
                        attacks_complete &= complete
                        used += 1
                    except ValueError:
                        attacks_complete = False
                best = member.get("bestOpponentAttack")
                extra = {"field_presence": sorted(member.keys() & {
                    "tag", "name", "townhallLevel", "mapPosition", "opponentAttacks", "attacks", "bestOpponentAttack"})}
                if best is not None:
                    try:
                        best_attack, _ = attack_projection(best, stub=True)
                        if best_attack["defender"] != tag:
                            raise ValueError("wrong_defender")
                        extra["best_opponent_attack_order"] = best_attack["attack_order"]
                        stubs.append(best_attack)
                    except ValueError:
                        attacks_complete = False
                members.append({"side": side, "player_tag": tag, "name": name, "townhall": townhall,
                                "source_map_position": position, "attacks_expected": per_member,
                                "attacks_used": len(outgoing), "opponent_attacks": optional_int(member, "opponentAttacks"),
                                "extra_json": canonical_json(extra)})
            except ValueError:
                roster_complete = attacks_complete = False
        roster_complete &= len(seen) == len(roster) == size
        attacks_complete &= projected["attacks"] is not None and used == projected["attacks"]
    membership = {m["player_tag"]: m["side"] for m in members}
    if len(membership) != len(members):
        roster_complete = attacks_complete = False
    for stub in stubs:
        if stub["attack_order"] not in attacks:
            attacks[stub["attack_order"]] = stub
            attacks_complete = False
        elif any(stub[key] is not None and stub[key] != attacks[stub["attack_order"]][key]
                 for key in ("attacker", "defender", "stars", "destruction")):
            collisions.add(stub["attack_order"])
    for order in collisions:
        attacks.pop(order, None)
        attacks_complete = False
    for attack in attacks.values():
        if (attack["attacker"] not in membership or attack["defender"] not in membership
                or membership[attack["attacker"]] == membership[attack["defender"]]):
            attacks_complete = False
    # opponentAttacks can lag or disagree with the final outgoing records.
    # Preserve that counter, but prove coverage from both side totals and the
    # complete global order instead of treating it as another attack list.
    if set(attacks) != set(range(1, len(attacks) + 1)):
        attacks_complete = False
    modifier = raw.get("battleModifier")
    if modifier is not None and not isinstance(modifier, str):
        raise ValueError("invalid_battle_modifier")
    return {"type": war_type(raw, bool(war_tag)), "war_tag": war_tag,
            "preparation_at": preparation, "start_at": start, "war_start_at": war_start, "end_at": end, "state": state,
            "team_size": size, "attacks_per_member": per_member,
            "battle_modifier_json": canonical_json(modifier) if modifier is not None else None,
            "field_presence_json": canonical_json(sorted(raw.keys() & {"state", "teamSize", "attacksPerMember",
                "preparationStartTime", "startTime", "warStartTime", "endTime", "battleModifier", "type", "isFriendly", "result"})),
            "roster_complete": bool(roster_complete), "attacks_complete": bool(attacks_complete and roster_complete),
            "result_complete": state == "warEnded" and all(
                p["stars"] is not None and p["destruction"] is not None for p, _ in sides),
            "sides": [p for p, _ in sides], "members": sorted(members, key=lambda m: (m["side"], m["player_tag"])),
            "attacks": sorted(attacks.values(), key=lambda a: a["attack_order"]), "aggregate": aggregate}


def project_log_entry(raw):
    """Distinguish a league-week total from a single two-clan war."""
    if not isinstance(raw, dict):
        raise ValueError("invalid_war")
    opponent = raw.get("opponent")
    if not isinstance(opponent, dict) or "tag" in opponent:
        return project_war(raw, aggregate=True)
    # This is the API's CWL summary shape, not a deleted or malformed clan.
    if (raw.get("attacksPerMember") != 1 or type(raw.get("attacksPerMember")) is not int
            or not nonnegative_int(raw.get("teamSize")) or raw["teamSize"] < 1
            or not {"badgeUrls", "clanLevel", "stars", "destructionPercentage"} <= opponent.keys()
            or type(opponent["clanLevel"]) is not int or opponent["clanLevel"] != 0
            or not number(opponent["destructionPercentage"], 0)):
        raise ValueError("invalid_cwl_summary")
    clan = side_projection(raw.get("clan"), destruction_maximum=100 * CWL_ROUNDS)
    end = api_time(raw.get("endTime"))
    result, modifier = raw.get("result"), raw.get("battleModifier")
    if (end is None or result not in (None, "win", "lose", "tie")
            or modifier is not None and not isinstance(modifier, str)
            or clan["destruction"] is None or clan["stars"] is None or clan["attacks"] is None
            or clan["attacks"] > raw["teamSize"] * CWL_ROUNDS):
        raise ValueError("invalid_cwl_summary")
    opponent_stars = optional_int(opponent, "stars")
    if opponent_stars is None:
        raise ValueError("invalid_cwl_summary")
    return {"cwl_summary": True, "clan_tag": clan["clan_tag"], "end_at": end,
            "team_size": raw["teamSize"], "attacks_per_member": 1, "result": result,
            "battle_modifier_json": canonical_json(modifier) if modifier is not None else None,
            **{key: clan[key] for key in ("name", "level", "badge_json", "attacks", "stars", "destruction", "xp_earned")},
            "opponent_level": 0, "opponent_badge_json": badge(opponent),
            "opponent_stars": opponent_stars, "opponent_destruction": opponent["destructionPercentage"]}
