"""Offline war parity rehearsal: frozen old clan health storage against the record's war reads.

Only regular wars present in both are compared. Old clan health keeps no
scores or results, so the clan's stars are derived from its stored attacks
the way Clash scores them (best attack per defender). The old file is
opened read-only. Every difference is returned with its likely reason.
"""

from collections import defaultdict
from contextlib import closing
import sqlite3

from elbow_helper.data.store.wars import END_DRIFT, SECOND


def old_regular_wars(path):
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        wars = [dict(row) for row in connection.execute("SELECT * FROM wars WHERE war_type='REG' ORDER BY end_ts")]
        roster, attacks = defaultdict(dict), defaultdict(list)
        for row in connection.execute("SELECT * FROM war_roster_members"):
            roster[(row["war_id"], row["clan_code"])][row["player_tag"]] = dict(row)
        for row in connection.execute("SELECT * FROM war_attacks WHERE war_type='REG'"):
            attacks[(row["war_id"], row["clan_code"])].append(dict(row))
    return wars, roster, attacks


async def exhaust(read, *args, **kwargs):
    rows, offset = [], 0
    while True:
        result = await read(*args, **kwargs, offset=offset)
        rows.extend(result["rows"])
        if result["next_offset"] is None:
            return rows
        offset = result["next_offset"]


async def compare_frozen_wars(reads, old_path, clans, *, as_of):
    """Compare each old regular war with the record war of the same clan pair and end.

    ``clans`` maps old clan codes to clan tags. Returns matched wars, old-only
    wars, record-only wars and the differences of matched wars.
    """
    wars, roster, attacks = old_regular_wars(old_path)
    differences, matched, only_old, only_record = [], [], [], []
    for code, tag in clans.items():
        own = [war for war in wars if war["clan_code"] == code and war["end_ts"]]
        if not own:
            continue
        after = own[0]["end_ts"] * SECOND - END_DRIFT
        before = own[-1]["end_ts"] * SECOND + END_DRIFT + 1
        window = (after, min(before, as_of + 1)) if after < as_of else None
        record = await exhaust(reads.completed, [tag], *window, types=["REG", "FRIENDLY"], as_of=as_of) if window else []
        people = await exhaust(reads.participation, [tag], *window, types=["REG", "FRIENDLY"], as_of=as_of) if window else []
        by_war = defaultdict(dict)
        for row in people:
            by_war[row["war_id"]][row["player_tag"]] = row
        used = set()
        for old in own:
            candidates = [war for war in record if war["opponent"]["clan_tag"] == old["opponent_tag"]
                          and abs(war["end_at"] - old["end_ts"] * SECOND) <= END_DRIFT]
            if len(candidates) != 1:
                only_old.append({"clan_code": code, "old_war_id": old["war_id"], "state": old["state"],
                                 "reason": "ambiguous" if candidates else
                                 "not yet ended at as_of" if old["end_ts"] * SECOND > as_of else "not in record"})
                continue
            new = candidates[0]
            used.add(new["war_id"])
            matched.append({"clan_code": code, "old_war_id": old["war_id"], "war_id": new["war_id"]})
            key = (old["war_id"], code)

            def compare(field, old_value, new_value, reason):
                if old_value != new_value:
                    differences.append({"clan_code": code, "old_war_id": old["war_id"], "war_id": new["war_id"],
                                        "field": field, "old": old_value, "new": new_value, "reason": reason})

            coverage = new["coverage"]["own"]
            detail = ("record attack detail incomplete" if not coverage["attacks_complete"] else
                      "old capture not final" if old["state"] != "warEnded" else "unexplained")
            compare("type", "REG", new["type"], f"record decides by {new['type_basis']}; old labels every current war REG")
            compare("state", old["state"], new["state"],
                    "old missed the final capture" if new["state"] == "warEnded" else "record missed the final capture")
            compare("team_size", old["team_size"], new["team_size"], "unexplained")
            compare("attacks_per_member", old["attacks_per_member"], new["attacks_per_member"],
                    "old defaults a missing value to 1" if new["attacks_per_member"] is None else "unexplained")
            best = {}
            for attack in attacks[key]:
                best[attack["defender_tag"]] = max(best.get(attack["defender_tag"], 0), attack["stars"])
            compare("clan_stars", sum(best.values()), new["clan"]["stars"], detail)
            compare("clan_attacks_used", sum(r["attacks_used"] for r in roster[key].values()),
                    new["clan"]["attacks_used"], detail)
            accounts = by_war[new["war_id"]]
            compare("accounts", sorted(roster[key]), sorted(accounts),
                    "record lineup incomplete" if not coverage["lineup_complete"] else "unexplained")
            per_account = defaultdict(lambda: {"stars": 0, "triples": 0})
            for attack in attacks[key]:
                per_account[attack["player_tag"]]["stars"] += attack["stars"]
                per_account[attack["player_tag"]]["triples"] += attack["stars"] == 3
            for tag_ in sorted(roster[key].keys() & accounts.keys()):
                old_row, new_row = roster[key][tag_], accounts[tag_]
                compare(f"{tag_}/attacks_used", old_row["attacks_used"], new_row["attacks_used"], detail)
                compare(f"{tag_}/attacks_expected", old_row["attacks_expected"], new_row["attacks_expected"],
                        "old defaults a missing value to 1" if new_row["attacks_expected"] is None else "unexplained")
                compare(f"{tag_}/stars", per_account[tag_]["stars"], new_row["stars"], detail)
                compare(f"{tag_}/triples", per_account[tag_]["triples"], new_row["triples"], detail)
        only_record.extend({"clan_code": code, "war_id": war["war_id"], "type": war["type"]}
                           for war in record if war["war_id"] not in used)
    return {"matched": matched, "only_old": only_old, "only_record": only_record, "differences": differences}
