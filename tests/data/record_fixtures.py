"""Synthetic stored history for read and workflow tests, without old-file imports."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from elbow_helper.data.links import LinkService
from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store.battles import _find, identity, save_battle
from elbow_helper.data.store.battle_coverage import observe, save_watch, watch_fact
from elbow_helper.data.store.clans import collect_clans
from elbow_helper.data.store.link_state import FIELDS, TOPIC, apply_rows, digest, display_rows, receipt
from elbow_helper.data.store.people import mirror_links
from elbow_helper.data.store.projections import canonical_json, search_key
from elbow_helper.data.store.sources import record_source
from elbow_helper.data.store.war_completeness import upgrade_cwl_completeness
from tests.features.cwl_record_stub import LeagueRecord, attack, league_war, member

from .link_fixtures import CLANS
from .test_family_store import link, player, response
from .test_wars import END, SECOND, WarFixture


START = datetime(2026, 10, 16, tzinfo=timezone.utc)
BASE = int(START.timestamp()) * SECOND
DAY = 86400 * SECOND
FROZEN = END + 30 * DAY
WEEK_END = END // SECOND - 20 * 86400


def row(tag="#P0", owner=100, name=None):
    return dict(zip(FIELDS, (tag, owner, name, "#QQ", None, "")))


class ProcessCrash(BaseException):
    """A process stop cannot run in-process recovery."""


class PeopleFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.legacy = self.root / "links.sqlite3"
        # A frozen reference for lasting facade ordering and SQL adapter tests.
        from elbow_helper.features.account_links.schema import DATABASE
        DATABASE.initialize(self.legacy)
        initial = [row(), row("#Q0", name=""), row("#R0", 200, "Outside")]
        with closing(sqlite3.connect(self.legacy)) as c, c:
            c.execute("CREATE TABLE links (player_tag TEXT PRIMARY KEY, discord_user_id INTEGER NOT NULL, "
                      "player_name_last_seen TEXT, last_seen_clan_tag TEXT, last_seen_clan_code TEXT, last_seen_role TEXT)")
            c.executemany("INSERT INTO links VALUES (?,?,?,?,?,?)", [tuple(r.values()) for r in initial])
            c.execute("INSERT INTO ignored_tags VALUES ('#C0')")
            c.execute("INSERT INTO suggestions VALUES ('#G0','Candidate','ONE','#PP',100,NULL,1,2)")
        database = patch("elbow_helper.features.account_links.database.DB_PATH", self.legacy)
        database.start()
        self.addCleanup(database.stop)
        self.store = FamilyStore(self.root / "family.sqlite3")
        await self.store.initialize()
        self.service = LinkService(self.store)
        self.addAsyncCleanup(self.service.close)
        await self.clans()

        def seed(c):
            item = receipt(c, "people_membership_import", TOPIC, 200, len(initial))
            rows = {r["player_tag"]: r for r in initial}
            apply_rows(c, rows, 200, item, basis="legacy_current")
            display_rows(c, rows)
            for r in initial:
                if r["player_name_last_seen"] is not None:
                    account = c.execute("SELECT id FROM accounts WHERE player_tag=?", (r["player_tag"],)).fetchone()[0]
                    c.execute("INSERT INTO legacy_link_aliases VALUES (?,?,?,?,NULL,'legacy_unknown_age',?)",
                              (account, 200, r["player_name_last_seen"], search_key(r["player_name_last_seen"]), item))
            c.execute("INSERT INTO imports VALUES (?,1,'synthetic',200,?,200,'{}',?)", (TOPIC, digest(rows), item))
            c.execute("INSERT INTO link_imports(source_version,source_digest,frozen_at,basis,checks_json,source_item_id) "
                      "VALUES (1,'synthetic',200,'initial','{}',?)", (item,))
            c.commit()
        await self.store.call(seed)

    async def clans(self, at=100):
        clans = tuple(CLANS.values())
        specs = [(kind, clan.tag) for clan in clans for kind in ("clan_profile", "clan_roster")]
        run = await self.store.begin_run("clans", "family_clans", specs, at)
        responses = [response({"tag": clan.tag, "name": clan.name, "members": int(i < 2),
                              "memberList": [player("#P0" if i == 0 else "#Q0")] if i < 2 else []})
                     for i, clan in enumerate(clans)]
        await self.store.call(collect_clans, run, clans, responses, at)
        await self.store.finish(run)

    async def rows(self, sql, args=()):
        return await self.store.call(lambda c: [dict(r) for r in c.execute(sql, args)])

    async def execute(self, sql, args=()):
        def write(c):
            with c:
                c.execute(sql, args)
        await self.store.call(write)

    async def dump(self):
        return await self.store.call(lambda c: tuple(c.iterdump()))


def battle(offset=-10, *, mode="ranked", attack=True, **extra):
    return {"battleTimestamp": (START + timedelta(seconds=offset)).strftime("%Y%m%dT%H%M%S.000Z"),
            "battleType": mode, "attack": attack, "opponentPlayerTag": "#Q0", "opponentName": "Opponent",
            "stars": 3, "destructionPercentage": 100, "opponentTownHallLevel": 16, "armyShareCode": None,
            "lootedResources": [{"name": "Gold", "amount": 123}], "future": {"null": None, "empty": []}, **extra}


class RankedFixture(WarFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        run = await self.store.begin_run("links", "current", [("account_links", "current")], BASE)
        await self.store.call(mirror_links, run, link(), BASE)
        await self.store.finish(run)
        self.historical_battles = []
        self.polls = [(0, "ok", 0, 0)]

    def poll(self, offset=0, status="ok", entries=0, skipped=0):
        self.polls.append((offset, status, entries, skipped))

    def add_battle(self, raw=None, *, key="old-key", clan="#LQ"):
        raw = battle() if raw is None else raw
        self.historical_battles.append((raw, clan))
        return raw

    async def seed_history(self):
        def seed(c):
            item = receipt(c, "synthetic_history", "ranked", BASE + SECOND, len(self.historical_battles))
            run = c.execute("SELECT run_id FROM collection_items WHERE id=?", (item,)).fetchone()[0]
            for raw, clan in self.historical_battles:
                save_battle(c, "#P0", raw, run, BASE)
                stamp, opponent, direction = json.loads(identity(raw))
                stored = _find(c, "#P0", stamp, opponent, direction)
                provenance = {"clan_when_first_seen": clan, "observed_at": BASE,
                              "basis": "clan_when_first_seen", "source": "legacy_ranked"}
                c.execute("UPDATE family_battles SET legacy_first_seen_json=? WHERE id=?",
                          (canonical_json(provenance), stored["id"]))
            fact = watch_fact()
            for offset, status, entries, skipped in sorted(self.polls):
                observe(fact, BASE + offset * SECOND, status, entries, skipped)
            save_watch(c, "#P0", "legacy_ranked", fact, run, run)
            c.commit()
        await self.store.call(seed)

    async def live(self, raw, offset, status=200):
        run = await self.store.begin_run("battle_logs", "family_api", [("battle_log", "#P0")], BASE + offset * SECOND)
        await self.store.requested(run, 0)
        await self.store.call(record_source, run, 0, {"source_kind": "battle_log", "scope_id": "#P0"},
                              response(raw, status) if status else None, BASE + offset * SECOND)
        await self.store.finish(run)


class CwlFixture(WarFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        clans = {"ONE": SimpleNamespace(tag="#PY"), "TWO": SimpleNamespace(tag="#LQ")}
        self.league_record = LeagueRecord(self.path.parent, clans)
        self.league_record.store = self.store

    async def week(self, *, code="ONE", tag="#PY", prefix="#RR", opponents=("#C0", "#C2", "#C8"),
                   lineup=("#P0", "#P2"), season="2026-10", label="2026-10-01", league="Crystal League I",
                   first_end=WEEK_END, source="war_cwl_api", positions=(1, 2), defender_positions=(1, 2)):
        wars = []
        for index, round_no in enumerate((1, 2, 3)):
            members = [member(player, name="Old", position=position, used=int(not (order == 2 and round_no == 2)))
                       for order, (player, position) in enumerate(zip(lineup, positions), 1)]
            hits = [attack(order, row["tag"], ("#Q0", "#Q2")[order-1], stars=3 if order == 1 else 2,
                           destruction=100.0 if order == 1 else 70.0, townhall=15+order,
                           position=defender_positions[order-1])
                    for order, row in enumerate(members, 1) if row["used"]]
            wars.append(league_war(prefix + "028"[index], round_no, first_end + index * 86400,
                                   members, hits, opponent=opponents[index]))
        await self.league_record.week(code, season, wars, league=league)

        def metadata(c):
            season_row = c.execute("SELECT id,group_metadata_json FROM cwl_seasons ORDER BY id DESC LIMIT 1").fetchone()
            metadata = json.loads(season_row[1])
            metadata["legacy"].update(season_label=label, sources=[source])
            c.execute("UPDATE cwl_seasons SET first_seen_at=?,final_at=?,group_metadata_json=? WHERE id=?",
                      (FROZEN, FROZEN, canonical_json(metadata), season_row[0]))
            for war in wars:
                c.execute("UPDATE war_sides SET extra_json=replace(extra_json,'synthetic',?) WHERE war_id="
                          "(SELECT id FROM wars WHERE war_tag=?)", (source, war["tag"]))
                c.execute("UPDATE war_members SET name='Them' WHERE side=(SELECT side FROM war_sides WHERE "
                          "war_id=war_members.war_id AND clan_tag=?)", (war["opponent"],))
            upgrade_cwl_completeness(c)
            c.commit()
        await self.store.call(metadata)
        return ["CWL:" + war["tag"] for war in wars]


def seed_war_alias(connection, source, legacy_id, clan_code, war_id, *, at):
    connection.execute("INSERT INTO war_legacy_ids(source,legacy_id,clan_code,war_id,mapped_at) VALUES (?,?,?,?,?)",
                       (source, legacy_id, clan_code, war_id, at))
