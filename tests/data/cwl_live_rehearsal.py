"""Test-only rehearsal of the CWL copy on the live bot's path, on copies of the old and family files.

Run from the repository root:
``python -m tests.data.cwl_live_rehearsal <data folder> <work folder> <main code folder> [report file]``.
The main code folder holds main's ``elbow_helper`` package (the commit before the CWL copy). As on the live bot:
the old bot has written the running November league week into its store; main's code has copied the regular
wars; the collector has saved November into the record through its own pass (a fake Clash client answers it), with
round 1 ended, round 2 in battle day and round 3 in preparation. Then the bot starts with this code. A second
record repeats it with the collector holding a November round under a war tag the old store has from October.
"""

import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import logging
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote

from elbow_helper.configuration.clans import CLANS
from elbow_helper.data.collectors.service import FamilyCollectors
from elbow_helper.data.collectors.wars import due_jobs
from elbow_helper.data.store import FamilyStore, now_us, wars_import, wars_import_cwl
from elbow_helper.data.store.wars import SECOND
from elbow_helper.infrastructure.clash.client import ClashResponse

from .war_import_rehearsal import Lines, collectors, fingerprint, settle

CHARS = "0289PYLQGRJCUV"
SEASON = "2026-11"
# Two family clans, each in its own November group, as on the live bot.
GROUP_CLANS = ("BE1", "BEC")
MAIN_COPY = """
import asyncio, json, sys
from pathlib import Path
from elbow_helper.data.store import FamilyStore, wars_import
from elbow_helper.data.store.wars_import import copy_at_startup
async def main():
    store = FamilyStore(Path(sys.argv[1]))
    await store.initialize()
    result = await store.call(copy_at_startup, sys.argv[2], sys.argv[3])
    print(json.dumps({"code": wars_import.__file__, "wars": result["checks"]["wars"]["events"],
                      "cwl_outcomes": {table: counts.get("excluded:cwl", 0)
                                       for table, counts in result["checks"]["outcomes"].items()}}))
asyncio.run(main())
"""


def clash_time(moment):
    return moment.strftime("%Y%m%dT%H%M%S.000Z")


def hashed(path):
    return sha256(Path(path).read_bytes()).hexdigest()


class November:
    """One family clan's November group: 8 clans, 7 rounds, 3 of them known so far."""

    def __init__(self, code, group_index, roster, league, now, round_one_tag=None):
        self.code, self.tag = code, CLANS[code].tag
        g = "PL"[group_index]
        self.opponents = [(f"#9CC{g}{CHARS[i]}", f"Rival {i + 1}",
                           [(f"#9QQ{g}{CHARS[i]}{CHARS[j // 14]}{CHARS[j % 14]}", f"Rival player {j + 1}", 15)
                            for j in range(15)]) for i in range(7)]
        self.family = (self.tag, f"Family {code}", roster)
        self.league = league
        # Round 1 ended two hours ago, round 2 is in battle day, round 3 in preparation.
        end = now - timedelta(hours=2)
        self.times = [(end + timedelta(days=r - 2), end + timedelta(days=r - 1), end + timedelta(days=r))
                      for r in range(3)]
        self.rounds = [[f"#9VV{g}{CHARS[r]}{CHARS[w]}" for w in range(4)] for r in range(3)]
        if round_one_tag:
            self.rounds[0][0] = round_one_tag
        self.states = ("warEnded", "inWar", "preparation")

    def pairings(self, round_index):
        """The family clan meets opponent ``round_index``; the other six clans play each other."""
        others = [o for i, o in enumerate(self.opponents) if i != round_index]
        return [(self.family, self.opponents[round_index])] + [(others[i], others[i + 1]) for i in range(0, 6, 2)]

    def group(self):
        clans = [self.family, *self.opponents]
        return {"state": "inWar", "season": SEASON, "league": {"id": 48000000, "name": self.league},
                "clans": [{"tag": tag, "name": name, "clanLevel": 10, "badgeUrls": {"small": "https://example.test/b"},
                           "members": [{"tag": p, "name": n, "townHallLevel": th} for p, n, th in players]}
                          for tag, name, players in clans],
                "rounds": [{"warTags": tags} for tags in self.rounds] + [{"warTags": ["#0"] * 4}] * 4}

    def war(self, round_index, pair):
        state, (prep, start, end) = self.states[round_index], self.times[round_index]
        attacking = {"warEnded": 15, "inWar": 7, "preparation": 0}[state]
        attacks, order = ([], []), 1
        for i in range(attacking):
            for side in (0, 1):
                # One family player misses round 1.
                if side == 0 and pair[0] is self.family and state == "warEnded" and i == 14:
                    continue
                stars = (3, 2, 1)[i % 3] if side == 0 else 2
                attacks[side].append((i, {"attackerTag": pair[side][2][i][0], "defenderTag": pair[1 - side][2][i][0],
                                          "stars": stars, "destructionPercentage": (100.0, 80.0, 55.0)[i % 3]
                                          if side == 0 else 75.0, "order": order, "duration": 120}))
                order += 1
        sides = []
        for side, (tag, name, players) in enumerate(pair):
            mine, theirs = attacks[side], attacks[1 - side]
            members = []
            for i, (player, player_name, townhall) in enumerate(players):
                members.append({"tag": player, "name": player_name, "townhallLevel": townhall, "mapPosition": i + 1,
                                "opponentAttacks": sum(1 for j, _ in theirs if j == i),
                                "attacks": [a for j, a in mine if j == i]})
            sides.append({"tag": tag, "name": name, "clanLevel": 10, "badgeUrls": {"small": "https://example.test/b"},
                          "attacks": len(mine), "stars": sum(a["stars"] for _, a in mine),
                          "destructionPercentage": sum(a["destructionPercentage"] for _, a in mine) / 15,
                          "members": members})
        return {"state": state, "teamSize": 15, "attacksPerMember": 1, "preparationStartTime": clash_time(prep),
                "startTime": clash_time(start), "endTime": clash_time(end), "clan": sides[0], "opponent": sides[1]}

    def wars(self):
        return {tag: self.war(r, pair) for r in range(3) for tag, pair in zip(self.rounds[r], self.pairings(r))}

    def old_rows(self, old, captured):
        """What the old bot writes for the family clan's November wars so far."""
        for r in range(3):
            tag, payload = self.rounds[r][0], self.war(r, self.pairings(r)[0])
            prep, start, end = (int(t.timestamp()) for t in self.times[r])
            state, war_id = payload["state"], "CWL:" + tag
            old.execute("INSERT INTO wars(war_id,war_type,clan_code,clan_tag,opponent_tag,opponent_name,cwl_season,"
                        "cwl_season_label,cwl_league,cwl_round,team_size,attacks_per_member,state,preparation_start_ts,"
                        "start_ts,end_ts,last_seen_ts,source) VALUES (?,'CWL',?,?,?,?,?,?,?,?,15,1,?,?,?,?,?,"
                        "'war_cwl_api')", (war_id, self.code, self.tag, payload["opponent"]["tag"],
                                           payload["opponent"]["name"], SEASON, SEASON, self.league, r + 1, state,
                                           prep, start, end, captured))
            defenders = {m["tag"]: m for m in payload["opponent"]["members"]}
            for member in payload["clan"]["members"]:
                used = len(member["attacks"])
                old.execute("INSERT INTO war_roster_members(war_id,clan_code,player_tag,player_name,townhall,"
                            "map_position,attacks_expected,attacks_used,roster_state,captured_ts,source) "
                            "VALUES (?,?,?,?,?,?,1,?,?,?,'war_cwl_api')",
                            (war_id, self.code, member["tag"], member["name"], member["townhallLevel"],
                             member["mapPosition"], used, state, captured))
                old.execute("INSERT INTO war_activity(war_id,war_type,clan_code,clan_tag,end_ts,player_tag,player_name,"
                            "attacks_expected,attacks_used,stars,destruction,attack_count,source) "
                            "VALUES (?,'CWL',?,?,?,?,?,1,?,?,?,?,'war_cwl_api')",
                            (war_id, self.code, self.tag, end, member["tag"], member["name"], used,
                             sum(a["stars"] for a in member["attacks"]),
                             sum(a["destructionPercentage"] for a in member["attacks"]), used))
                for attack in member["attacks"]:
                    defender = defenders[attack["defenderTag"]]
                    old.execute("INSERT INTO war_attacks(war_id,war_type,clan_code,clan_tag,end_ts,war_state,player_tag,"
                                "player_name,attack_order,defender_tag,defender_name,defender_map_position,"
                                "defender_townhall,stars,destruction,fresh_attack,duration,source) "
                                "VALUES (?,'CWL',?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,'war_cwl_api')",
                                (war_id, self.code, self.tag, end, state, member["tag"], member["name"],
                                 attack["order"], defender["tag"], defender["name"], defender["mapPosition"],
                                 defender["townhallLevel"], attack["stars"], attack["destructionPercentage"],
                                 attack["duration"]))


class FakeClash:
    """Answers the collector's league requests from the synthetic November; every other request fails."""

    def __init__(self, groups):
        self.groups = {group.tag: group.group() for group in groups}
        self.wars = {tag: payload for group in groups for tag, payload in group.wars().items()}
        self.paths = []

    async def get(self, path, **kwargs):
        path = unquote(path)
        self.paths.append(path)
        if path.startswith("/clanwarleagues/wars/"):
            tag = path.rsplit("/", 1)[1]
            return ClashResponse(200, self.wars[tag], {}, 1, 2) if tag in self.wars else ClashResponse(
                404, {"reason": "notFound"}, {}, 1, 2)
        if path.endswith("/currentwar/leaguegroup"):
            tag = path.split("/")[2]
            return ClashResponse(200, self.groups[tag], {}, 1, 2) if tag in self.groups else ClashResponse(
                404, {"reason": "notFound"}, {}, 1, 2)
        return ClashResponse(503, {"reason": "inMaintenance"}, {}, 1, 2)


def rosters(health, family):
    """Each group clan's latest October lineup from the old store: real family accounts."""
    with closing(sqlite3.connect(f"file:{Path(health).as_posix()}?mode=ro", uri=True)) as old, \
            closing(sqlite3.connect(f"file:{Path(family).as_posix()}?mode=ro", uri=True)) as record:
        accounts = {row[0] for row in record.execute("SELECT player_tag FROM accounts")}
        result = {}
        for code in GROUP_CLANS:
            war_id, league = old.execute("SELECT war_id,cwl_league FROM wars WHERE war_type='CWL' AND clan_code=? "
                                         "ORDER BY end_ts DESC LIMIT 1", (code,)).fetchone()
            players = old.execute("SELECT player_tag,player_name,townhall FROM war_roster_members WHERE war_id=? "
                                  "AND clan_code=? ORDER BY map_position", (war_id, code)).fetchall()[:15]
            result[code] = ([tuple(p) for p in players], league, sum(p[0] in accounts for p in players))
        october = {code: old.execute("SELECT war_id FROM wars WHERE war_type='CWL' AND clan_code=? AND cwl_season='2026-10' "
                                     "AND cwl_round=1", (code,)).fetchone()[0][4:] for code in GROUP_CLANS}
        old_tags = {row[0][4:] for row in old.execute("SELECT war_id FROM wars WHERE war_type='CWL'")}
    return result, october, old_tags


def facts(connection):
    """The regular part and November, row by row, to compare before and after a start."""
    tags = [row[0] for row in connection.execute(
        "SELECT r.war_tag FROM cwl_rounds r JOIN cwl_seasons s ON s.id=r.season_id WHERE s.season_key=?", (SEASON,))]
    marks = ",".join("?" for _ in tags) or "''"
    wars = f"SELECT id FROM wars WHERE war_tag IN ({marks})"
    queries = {
        "regular_marker": ("SELECT * FROM imports WHERE topic='wars_regular'", ()),
        "regular_wars": ("SELECT * FROM wars WHERE type!='CWL' ORDER BY id", ()),
        "regular_sides": ("SELECT s.* FROM war_sides s JOIN wars w ON w.id=s.war_id WHERE w.type!='CWL' "
                          "ORDER BY s.war_id,s.side", ()),
        "regular_members": ("SELECT m.* FROM war_members m JOIN wars w ON w.id=m.war_id WHERE w.type!='CWL' "
                            "ORDER BY m.id", ()),
        "regular_attacks": ("SELECT a.* FROM war_attacks a JOIN wars w ON w.id=a.war_id WHERE w.type!='CWL' "
                            "ORDER BY a.war_id,a.attack_order", ()),
        "regular_aliases": ("SELECT * FROM war_legacy_ids WHERE legacy_label!='CWL' ORDER BY source,legacy_id,clan_code",
                            ()),
        "november_seasons": ("SELECT * FROM cwl_seasons WHERE season_key=? ORDER BY id", (SEASON,)),
        "november_clans": ("SELECT c.* FROM cwl_clans c JOIN cwl_seasons s ON s.id=c.season_id WHERE s.season_key=? "
                           "ORDER BY c.season_id,c.clan_tag", (SEASON,)),
        "november_players": ("SELECT p.* FROM cwl_players p JOIN cwl_seasons s ON s.id=p.season_id "
                             "WHERE s.season_key=? ORDER BY p.season_id,p.clan_tag,p.player_tag", (SEASON,)),
        "november_rounds": ("SELECT r.* FROM cwl_rounds r JOIN cwl_seasons s ON s.id=r.season_id WHERE s.season_key=? "
                            "ORDER BY r.season_id,r.round_no,r.war_tag", (SEASON,)),
        "november_wars": (f"SELECT * FROM wars WHERE war_tag IN ({marks}) ORDER BY id", tags),
        "november_sides": (f"SELECT * FROM war_sides WHERE war_id IN ({wars}) ORDER BY war_id,side", tags),
        "november_members": (f"SELECT * FROM war_members WHERE war_id IN ({wars}) ORDER BY id", tags),
        "november_attacks": (f"SELECT * FROM war_attacks WHERE war_id IN ({wars}) ORDER BY war_id,attack_order", tags),
        "november_polling": (f"SELECT * FROM war_collection_state WHERE (source_kind='cwl_war' AND scope_id IN ({marks})) "
                             "OR source_kind='cwl_group' ORDER BY source_kind,scope_id", tags),
    }
    return {name: [tuple(row) for row in connection.execute(sql, args)] for name, (sql, args) in queries.items()}


def summary(connection):
    return {
        "november_wars": [dict(row) for row in connection.execute(
            "SELECT r.round_no,w.war_tag,w.state,w.final_at IS NOT NULL AS final,s.enabled AS polled,"
            "(SELECT COUNT(*) FROM war_sides x WHERE x.war_id=w.id AND x.clan_tag IN (%s)) AS family_sides "
            "FROM cwl_rounds r JOIN cwl_seasons c ON c.id=r.season_id JOIN wars w ON w.id=r.war_id "
            "LEFT JOIN war_collection_state s ON s.source_kind='cwl_war' AND s.scope_id=w.war_tag "
            "WHERE c.season_key=? ORDER BY c.id,r.round_no,w.war_tag" % ",".join(f"'{c.tag}'" for c in CLANS.values()),
            (SEASON,))],
        "november_seasons": [dict(row) for row in connection.execute(
            "SELECT season_key,group_key,state,final_at FROM cwl_seasons WHERE season_key=?", (SEASON,))],
        "copied_seasons_open": connection.execute(
            "SELECT COUNT(*) FROM cwl_seasons WHERE season_key!=? AND final_at IS NULL", (SEASON,)).fetchone()[0],
        "copied_tags_polled": connection.execute(
            "SELECT COUNT(*) FROM war_collection_state s JOIN war_legacy_ids l ON l.legacy_id='CWL:'||s.scope_id "
            "WHERE s.source_kind='cwl_war' AND s.enabled=1").fetchone()[0],
    }


def jobs_at(path, at):
    """What the collector would poll at ``at``, asked of a throwaway copy so the record keeps no state rows."""
    peek = Path(path).with_name("peek.sqlite3")
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(peek)) as target:
        source.backup(target)
    with closing(sqlite3.connect(peek)) as connection:
        connection.row_factory = sqlite3.Row
        rows = due_jobs(connection, CLANS.values(), at)
        return polled(connection, rows)


def polled(connection, rows):
    copied = {row[0] for row in connection.execute("SELECT substr(legacy_id,5) FROM war_legacy_ids "
                                                   "WHERE legacy_label='CWL'")}
    codes = {clan.tag: code for code, clan in CLANS.items()}
    return {"cwl_group": sorted(codes.get(r["scope_id"], r["scope_id"]) for r in rows if r["source_kind"] == "cwl_group"),
            "cwl_war": sorted(r["scope_id"] for r in rows if r["source_kind"] == "cwl_war"),
            "copied_tags_polled": sum(r["source_kind"] == "cwl_war" and r["scope_id"] in copied for r in rows)}


async def collect_november(path, groups):
    """The collector's own pass saves November: group, round wars, unrelated pairings."""
    store = FamilyStore(path)
    client = FakeClash(groups)
    bot = SimpleNamespace(boot_complete=asyncio.Event(), guild_id=0, get_guild=lambda guild_id: None)
    report = await FamilyCollectors(bot, store, client, CLANS.values()).collect_wars()
    return store, report, client


async def rehearse(data, work, main_code):
    data, work, main_code = Path(data), Path(work), Path(main_code).resolve()
    work.mkdir(parents=True, exist_ok=True)
    health, cache = work / "clan_health.db", work / "war_cache.json"
    shutil.copyfile(data / "clan_health" / "clan_health.db", health)
    shutil.copyfile(data / "war" / "war_cache.json", cache)
    lines = Lines()
    logger = logging.getLogger("elbow_helper.data.collectors.service")
    logger.addHandler(lines)
    logger.setLevel(logging.INFO)
    report = {}
    now = datetime.fromtimestamp(now_us() / SECOND, timezone.utc).replace(microsecond=0)
    found, october, old_tags = rosters(health, data / "family" / "family.sqlite3")
    report["group_clans"] = {code: {"league": league, "players": len(players), "with_family_account": linked}
                             for code, (players, league, linked) in found.items()}
    groups = [November(code, i, *found[code][:2], now) for i, code in enumerate(GROUP_CLANS)]
    new_tags = {tag for group in groups for tags in group.rounds for tag in tags}
    report["november_tags_in_old_store"] = len(new_tags & old_tags)

    # The old bot keeps writing the running week into its own store.
    with closing(sqlite3.connect(health)) as old, old:
        for group in groups:
            group.old_rows(old, int(now.timestamp()))

    # Main's code copies the regular wars alone, as the live bot did.
    base = work / "base.sqlite3"
    shutil.copyfile(data / "family" / "family.sqlite3", base)
    started = time.perf_counter()
    main = subprocess.run([sys.executable, "-c", MAIN_COPY, str(base), str(health), str(cache)], cwd=main_code,
                          capture_output=True, text=True)
    if main.returncode:
        raise RuntimeError("main's regular copy failed:\n" + main.stderr[-3000:])
    report["main_regular_copy"] = {**json.loads(main.stdout.strip().splitlines()[-1]),
                                   "seconds": round(time.perf_counter() - started, 2)}
    report["main_regular_copy"]["code"] = str(Path(report["main_regular_copy"]["code"]).relative_to(main_code))
    store = await settle(base)
    await store.call(lambda c: c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall())
    conflict = work / "conflict.sqlite3"
    shutil.copyfile(base, conflict)

    # The collector saves November.
    live = work / "live.sqlite3"
    shutil.copyfile(base, live)
    store, run, client = await collect_november(live, groups)
    report["collector_pass"] = {key: run[key] for key in ("status", "succeeded", "empty", "failed")}
    report["collector_league_requests"] = sum(p.startswith("/clanwarleagues/") or p.endswith("leaguegroup")
                                              for p in client.paths)
    before = await store.call(facts)
    report["before_start"] = await store.call(summary)

    # The bot starts with this code.
    lines.lines.clear()
    started = time.perf_counter()
    with patch.object(wars_import, "write_regular", side_effect=AssertionError("regular part ran")):
        await collectors(store, health, cache)._copy_old_wars()
    report["start_seconds"] = round(time.perf_counter() - started, 2)
    report["start_log"] = lines.lines[:]
    after = await store.call(facts)
    report["unchanged"] = {name: before[name] == after[name] for name in before}
    report["rows_compared"] = {name: len(rows) for name, rows in before.items()}
    marker = await store.call(lambda c: c.execute("SELECT checks_json FROM imports WHERE topic=?",
                                                  (wars_import_cwl.TOPIC,)).fetchone())
    checks = json.loads(marker[0])
    report["cwl_copy"] = {"inserted": checks["inserted"], "left_out": checks["left_out"],
                          "outcomes": checks["outcomes"], "log_checks": checks["log_checks"]}
    report["after_start"] = await store.call(summary)
    soon = int((now + timedelta(hours=1)).timestamp()) * SECOND
    late = int(datetime(2026, 11, 20, 12, tzinfo=timezone.utc).timestamp()) * SECOND
    report["polled_next_hour"] = jobs_at(live, soon)
    report["polled_20_november"] = jobs_at(live, late)

    # The collector holds a November round under a war tag the old store has from October.
    clash = [November(code, i, *found[code][:2], now, round_one_tag=october[code] if code == GROUP_CLANS[0] else None)
             for i, code in enumerate(GROUP_CLANS)]
    store, run, _ = await collect_november(conflict, clash)
    await store.call(lambda c: c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall())
    shared = october[GROUP_CLANS[0]]
    saved = await store.call(lambda c: dict(c.execute(
        "SELECT war_tag,state,end_at,(SELECT COUNT(*) FROM war_members m WHERE m.war_id=w.id) AS members "
        "FROM wars w WHERE war_tag=?", (shared,)).fetchone()))
    record_before, old_before = fingerprint(conflict), hashed(health)
    results = []
    for attempt in (1, 2):
        lines.lines.clear()
        await collectors(FamilyStore(conflict), health, cache)._copy_old_wars()
        results.append({"log": lines.lines[:], "record_unchanged": fingerprint(conflict) == record_before,
                        "old_store_unchanged": hashed(health) == old_before})
    store = FamilyStore(conflict)
    report["conflict"] = {
        "shared_tag_in_record": {**saved, "end_at": datetime.fromtimestamp(saved["end_at"] / SECOND, timezone.utc).isoformat()},
        "starts": results,
        "november_war_after": await store.call(lambda c: dict(c.execute(
            "SELECT war_tag,state,end_at,(SELECT COUNT(*) FROM war_members m WHERE m.war_id=w.id) AS members "
            "FROM wars w WHERE war_tag=?", (shared,)).fetchone())) == saved,
        "cwl_marker": await store.call(lambda c: c.execute(
            "SELECT COUNT(*) FROM imports WHERE topic=?", (wars_import_cwl.TOPIC,)).fetchone()[0])}
    return report


if __name__ == "__main__":
    result = json.dumps(asyncio.run(rehearse(*sys.argv[1:4])), indent=1, sort_keys=True, default=str)
    if len(sys.argv) > 4:
        Path(sys.argv[4]).write_text(result, encoding="utf-8")
    else:
        print(result)
