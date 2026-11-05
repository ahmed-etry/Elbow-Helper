"""Legacy CWL columns, seconds and lineup positions for the league features."""

import json

SECOND = 1_000_000
SOURCE = "family_record"


def old_id(war_tag):
    return f"CWL:{war_tag}"


def seconds(value):
    return None if value is None else value // SECOND


def renumbered(members, keep):
    """Positions 1..n by stored map position, as the old store numbered a lineup.

    A side copied from old clan health, or one with an unknown position, keeps
    its stored positions.
    """
    if keep or any(member["source_map_position"] is None for member in members):
        return {member["id"]: member["source_map_position"] for member in members}
    ordered = sorted(members, key=lambda member: (member["source_map_position"], member["player_tag"]))
    return {member["id"]: index for index, member in enumerate(ordered, 1)}


def _war(row, code, clan, key, label, league):
    extra = json.loads(row["own_extra"])
    legacy = extra.get("legacy") if isinstance(extra.get("legacy"), dict) else {}
    return {"war_id": old_id(row["war_tag"]), "war_type": "CWL", "clan_code": code, "clan_tag": clan,
            "opponent_tag": row["opponent_tag"], "opponent_name": row["opponent_name"] or "",
            "cwl_season": key, "cwl_season_label": label, "cwl_league": league or "", "cwl_round": row["round_no"],
            "team_size": row["team_size"], "attacks_per_member": row["attacks_per_member"], "state": row["state"],
            "preparation_start_ts": seconds(row["preparation_at"]) or 0, "start_ts": seconds(row["start_at"]) or 0,
            "end_ts": seconds(row["end_at"]), "last_seen_ts": seconds(row["last_seen_at"]),
            "source": legacy.get("source") or SOURCE, "record_war_id": row["record_id"]}

def _details(rows, wars):
    """Each war's own lineup as roster rows and the clan's attacks as attack rows, in the old shapes."""
    for row, war in zip(rows, wars):
        participation = {member["player_tag"]: member for member in row["participation"]}
        own_extra, other_extra = json.loads(row["own_extra"]), json.loads(row["opponent_extra"])
        own = [m for m in row["members"] if m["side"] == row["own_side"]]
        other = [m for m in row["members"] if m["side"] != row["own_side"]]
        places = renumbered(own, "legacy" in own_extra)
        places.update(renumbered(other, "legacy" in other_extra or other_extra.get("partial")))
        by_id = {m["id"]: m for m in own + other}
        own_tags = {m["player_tag"]: m for m in own}
        captured = {}
        for member in own:
            legacy = json.loads(member["extra_json"]).get("legacy")
            captured[member["id"]] = (seconds(legacy["captured_at"]) if isinstance(legacy, dict)
                                      and legacy.get("captured_at") else war["last_seen_ts"])
        war["roster"] = [{
            "war_id": war["war_id"], "clan_code": war["clan_code"], "player_tag": m["player_tag"],
            "player_name": m["name"] or m["player_tag"], "townhall": m["townhall"] or 0,
            "map_position": places[m["id"]] or 0, "attacks_expected": m["attacks_expected"],
            "attacks_used": m["attacks_used"], "roster_state": war["state"], "captured_ts": captured[m["id"]],
            "missed": participation[m["player_tag"]]["missed"],
            "source": war["source"]} for m in sorted(own, key=lambda m: (places[m["id"]] or 0, m["name"] or ""))]
        first = {}
        for attack in row["attacks"]:
            extra = json.loads(attack["extra_json"])
            defender = by_id[attack["defender_member_id"]]["player_tag"] if attack["defender_member_id"] in by_id \
                else extra.get("defender_tag")
            first.setdefault(defender, attack["attack_order"])
        war["attacks"] = []
        for attack in row["attacks"]:
            extra = json.loads(attack["extra_json"])
            attacker = by_id.get(attack["attacker_member_id"])
            tag = attacker["player_tag"] if attacker else extra.get("attacker_tag")
            if tag not in own_tags or attacker is not None and attacker["side"] != row["own_side"]:
                continue
            attacker = own_tags[tag]
            defender = by_id.get(attack["defender_member_id"])
            defender_tag = defender["player_tag"] if defender else extra.get("defender_tag")
            legacy = extra.get("legacy") if isinstance(extra.get("legacy"), dict) else {}
            fresh = legacy["fresh_attack"] if "fresh_attack" in legacy else first.get(defender_tag) == attack["attack_order"]
            war["attacks"].append({
                "war_id": war["war_id"], "war_type": "CWL", "clan_code": war["clan_code"],
                "clan_tag": war["clan_tag"], "end_ts": war["end_ts"], "war_state": war["state"],
                "player_tag": tag, "player_name": attacker["name"] or tag, "attack_order": attack["attack_order"],
                "defender_tag": defender_tag or "", "defender_name": (defender["name"] if defender else None)
                or defender_tag or "Unknown",
                "defender_map_position": (places.get(defender["id"]) if defender else None) or 0,
                "defender_townhall": (defender["townhall"] if defender else None) or 0,
                "stars": attack["stars"], "destruction": attack["destruction"], "fresh_attack": int(bool(fresh)),
                "duration": attack["duration"] or 0, "source": war["source"]})
        war["attacks"].sort(key=lambda attack: (attack["attack_order"], attack["player_name"]))



def feature_week(week, codes):
    """Adapt stored facts to the unchanged league history interface."""
    clan = week["clan_tag"]
    code = codes.get(clan, "")
    wars = [_war(row, code, clan, week["season_key"], week["label"], week["league_name"])
            for row in week["wars"]]
    for row, war in zip(week["wars"], wars):
        war["coverage"] = row["coverage"]
    if wars and "members" in week["wars"][0]:
        _details(week["wars"], wars)
    return {**week, "season": week["season_key"], "league": week["league_name"] or "",
            "clan_code": code, "wars": wars}
