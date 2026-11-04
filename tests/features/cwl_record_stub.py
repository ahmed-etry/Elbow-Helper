"""Synthetic league weeks written straight into a family record, shaped as weeks copied from old clan health.

Collector-saved weeks are covered in ``test_cwl_record``; tests about what the
league features do with stored weeks build them here, with any rounds, team
sizes or missing lineups they need. Times are UTC seconds.
"""

from __future__ import annotations

import json
from pathlib import Path

from elbow_helper.configuration.clans import CLANS
from elbow_helper.data.reads import CwlReads
from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store.link_state import receipt
from elbow_helper.features.cwl.queries import CwlQueries
from elbow_helper.features.cwl.record import CwlHistory

SECOND = 1_000_000


def member(tag, *, name=None, townhall=16, position=None, expected=1, used=1):
    return {"tag": tag, "name": name or f"Synthetic {tag}", "townhall": townhall, "position": position,
            "expected": expected, "used": used}


def attack(order, attacker, defender, *, stars=3, destruction=100.0, townhall=16, position=None, fresh=True):
    return {"order": order, "attacker": attacker, "defender": defender, "stars": stars, "destruction": destruction,
            "townhall": townhall, "position": position, "fresh": fresh}


def league_war(tag, round_no, end, lineup, attacks=(), *, opponent="#Q0", state="warEnded", team_size=None,
               attacks_per_member=1, start=None):
    return {"tag": tag, "round": round_no, "end": end, "lineup": list(lineup), "attacks": list(attacks),
            "opponent": opponent, "state": state, "team_size": team_size if team_size is not None else len(lineup),
            "attacks_per_member": attacks_per_member, "start": start if start is not None else end - 86400}


class LeagueRecord:
    """A family record holding synthetic league weeks, and the league reads over it."""

    def __init__(self, directory, clans=None):
        self.path = Path(directory) / "family.sqlite3"
        self.store = FamilyStore(self.path)
        self.clans = CLANS if clans is None else clans
        self.weeks = 0

    async def initialize(self):
        await self.store.initialize()
        return self

    def reads(self):
        return CwlReads(self.store, self.clans)

    def history(self, directory=None):
        return CwlHistory(self.reads(), directory, self.clans)

    def queries(self, **kwargs):
        return CwlQueries(self.history(), lambda: {}, **kwargs)

    async def week(self, code, season, wars, *, league=None, final=True):
        """One clan's league week; each war as made by ``league_war``."""
        self.weeks += 1
        clan, number = self.clans[code].tag, self.weeks

        def write(c):
            run = c.execute("SELECT run_id FROM collection_items WHERE id=?",
                            (receipt(c, "test", "test", 0, 0),)).fetchone()[0]
            ends = [war["end"] * SECOND for war in wars]
            metadata = {"round_count": len(wars), "expected_round_count": len(wars), "wars_per_round": 1,
                        "placeholder_rounds": [], "field_presence": [],
                        "legacy": {"season_key": season, "season_label": season, "league": league,
                                   "sources": ["synthetic"]}}
            season_id = c.execute(
                "INSERT INTO cwl_seasons(season_key,group_key,state,league_id,league_name,first_seen_at,final_at,"
                "group_metadata_json,source_run_id) VALUES (?,?,'ended',NULL,?,?,?,?,?)",
                (season, f"{clan}:{number}", league, min(ends, default=0), max(ends, default=0) if final else None,
                 json.dumps(metadata), run)).lastrowid
            c.execute("INSERT INTO cwl_season_sources VALUES (?,?)", (season_id, clan))
            for war in wars:
                end, start = war["end"] * SECOND, war["start"] * SECOND
                war_id = c.execute(
                    "INSERT INTO wars(type,type_basis,war_tag,preparation_at,start_at,end_at,state,team_size,"
                    "attacks_per_member,first_seen_at,last_seen_at,final_at,roster_complete,attacks_complete,"
                    "result_complete,content_hash,field_presence_json,source_run_id) "
                    "VALUES ('CWL','war_tag',?,?,?,?,?,?,?,?,?,NULL,1,1,0,'synthetic','[]',?)",
                    (war["tag"], start - 86400 * SECOND, start, end, war["state"], war["team_size"],
                     war["attacks_per_member"], min(start, end), end, run)).lastrowid
                pair = sorted((clan, war["opponent"]))
                sides = {tag: index for index, tag in enumerate(pair)}
                for tag, side in sides.items():
                    own = tag == clan
                    extra = ({"legacy": {"source": "synthetic"}, "derived": ["attacks"]} if own
                             else {"partial": True, "legacy": {"source": "synthetic"}})
                    used = sum(row["used"] or 0 for row in war["lineup"]) if own else None
                    c.execute("INSERT INTO war_sides(war_id,side,clan_tag,name,attacks,extra_json,source_run_id) "
                              "VALUES (?,?,?,?,?,?,?)", (war_id, side, tag, None if own else "Rival", used,
                                                         json.dumps(extra), run))
                ids = {}
                for row in war["lineup"]:
                    ids[row["tag"]] = c.execute(
                        "INSERT INTO war_members(war_id,side,player_tag,name,townhall,source_map_position,"
                        "attacks_expected,attacks_used,extra_json,source_run_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (war_id, sides[clan], row["tag"], row["name"], row["townhall"], row["position"],
                         row["expected"], row["used"], json.dumps({"legacy": {"captured_at": end}}), run)).lastrowid
                for hit in war["attacks"]:
                    if hit["defender"] not in ids:
                        ids[hit["defender"]] = c.execute(
                            "INSERT INTO war_members(war_id,side,player_tag,name,townhall,source_map_position,"
                            "extra_json,source_run_id) VALUES (?,?,?,?,?,?,?,?)",
                            (war_id, sides[war["opponent"]], hit["defender"], "Base", hit["townhall"],
                             hit["position"], json.dumps({"partial": True, "legacy": {"from_attacks": True}}),
                             run)).lastrowid
                    c.execute("INSERT INTO war_attacks(war_id,attack_order,attacker_member_id,defender_member_id,stars,"
                              "destruction,duration,extra_json,source_run_id) VALUES (?,?,?,?,?,?,90,?,?)",
                              (war_id, hit["order"], ids.get(hit["attacker"]), ids[hit["defender"]], hit["stars"],
                               hit["destruction"], json.dumps({"legacy": {"fresh_attack": hit["fresh"]}}), run))
                c.execute("INSERT INTO cwl_rounds(season_id,round_no,war_tag,war_id,source_run_id) VALUES (?,?,?,?,?)",
                          (season_id, war["round"], war["tag"], war_id, run))
                c.execute("INSERT INTO war_legacy_ids(source,legacy_id,clan_code,war_id,mapped_at) "
                          "VALUES ('clan_health',?,?,?,0)", (f"CWL:{war['tag']}", code, war_id))
            c.commit()

        await self.store.call(write)

    async def execute(self, sql, args=()):
        def write(c):
            c.execute(sql, args)
            c.commit()
        await self.store.call(write)
