"""War source status, type basis, legacy IDs and war coverage on the family record."""

import asyncio
from contextlib import closing
import sqlite3
import unittest

from elbow_helper.data.reads.coverage import WAR_CADENCES, envelope, war_side_coverage, war_signal
from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store.legacy_wars import map_legacy_war
from elbow_helper.data.store.schema import MIGRATIONS, SCHEMA_VERSION
from elbow_helper.data.store.war_projection import HOUR
from elbow_helper.infrastructure.persistence.migrations import run_sqlite_migrations

from .test_wars import END, SECOND, WarFixture, war, war_log


DAY = 24 * HOUR


class WarUpgradeTests(WarFixture, unittest.IsolatedAsyncioTestCase):
    async def test_upgrade_decides_every_type_and_keeps_facts(self):
        path = self.path.parent / "old.sqlite3"

        def old_record():
            with closing(sqlite3.connect(path)) as c:
                run_sqlite_migrations(c, MIGRATIONS[:8], target_version=8)
                c.execute("INSERT INTO collection_runs(id,collector,scope_kind,scheduled_at,started_at,status,expected) "
                          "VALUES (1,'wars','family_wars_and_leagues',1,1,'ok',1)")
                wars = ((1, "UNKNOWN", None, 0, 23 * HOUR, 47 * HOUR, ()),
                        (2, "UNKNOWN", None, 0, 2 * HOUR, 26 * HOUR, ()),
                        (3, "UNKNOWN", None, 0, 23 * HOUR, 49 * HOUR, ("war_log",)),
                        (4, "REG", None, None, None, 60 * HOUR, ("war_log",)),
                        (5, "CWL", "#R0", 0, 23 * HOUR, 47 * HOUR, ("cwl",)))
                for war_id, kind, tag, preparation, start, end, keys in wars:
                    c.execute("INSERT INTO wars(id,type,war_tag,preparation_at,start_at,end_at,state,first_seen_at,"
                              "last_seen_at,roster_complete,attacks_complete,result_complete,content_hash,"
                              "field_presence_json,source_run_id) VALUES (?,?,?,?,?,?,'warEnded',1,1,0,0,1,'old','[]',1)",
                              (war_id, kind, tag, preparation, start, end))
                    for side, clan in enumerate(("#GR", "#PY") if war_id != 5 else ("#CU", "#PY")):
                        c.execute("INSERT INTO war_sides(war_id,side,clan_tag,extra_json,source_run_id) VALUES (?,?,?,'{}',1)",
                                  (war_id, side, clan))
                    for key in keys:
                        c.execute("INSERT INTO war_source_keys VALUES (?,?,?)", (key, f"#GR:#PY:{end}:{war_id}", war_id))
                for kind in ("current_war", "war_log"):
                    c.execute("INSERT INTO war_collection_state(source_kind,scope_id,next_due_at) VALUES (?,'#PY',?)",
                              (kind, 10 ** 18))
                c.commit()
                return [tuple(row) for row in c.execute("SELECT * FROM wars ORDER BY id")]

        before = await asyncio.to_thread(old_record)
        upgraded = FamilyStore(path)
        await upgraded.initialize()

        def read(c):
            return (c.execute("PRAGMA user_version").fetchone()[0],
                    [tuple(row) for row in c.execute("SELECT * FROM wars ORDER BY id")],
                    {row["source_kind"]: dict(row) for row in c.execute("SELECT * FROM war_collection_state")},
                    c.execute("PRAGMA foreign_key_check").fetchall(), c.execute("PRAGMA integrity_check").fetchone()[0],
                    c.execute("SELECT COUNT(*) FROM war_legacy_ids").fetchone()[0])
        version, after, states, foreign, integrity, legacy = await upgraded.call(read)
        self.assertEqual((version, foreign, integrity, legacy), (SCHEMA_VERSION, [], "ok", 0))
        self.assertEqual([(row[1], row[-1]) for row in after], [
            ("REG", "times"), ("FRIENDLY", "times"), ("REG", "war_log"), ("REG", "war_log"), ("CWL", "war_tag")])
        # Only the type changes; every other fact keeps its value and provenance.
        self.assertEqual([row[:1] + row[2:] for row in before], [row[:1] + row[2:-1] for row in after])
        # Each public log is read again on the next pass to judge absence.
        self.assertEqual((states["war_log"]["next_due_at"], states["current_war"]["next_due_at"]), (0, 10 ** 18))
        self.assertEqual((states["current_war"]["last_attempt_at"], states["current_war"]["observed_json"]), (None, "{}"))
        await FamilyStore(path).initialize()
        self.assertEqual(after, (await upgraded.call(read))[1])


    async def test_upgrade_merges_log_doubles_into_their_current_war_copy(self):
        path = self.path.parent / "doubles.sqlite3"
        end = 47 * HOUR

        def old_record():
            with closing(sqlite3.connect(path)) as c:
                run_sqlite_migrations(c, MIGRATIONS[:8], target_version=8)
                c.execute("INSERT INTO collection_runs(id,collector,scope_kind,scheduled_at,started_at,status,expected) "
                          "VALUES (1,'wars','family_wars_and_leagues',1,1,'ok',1)")
                # Current-war copy, its log copy two seconds later, two current-war copies of
                # one pair that must stay apart with a log copy that matches both, and a lone log entry.
                wars = ((10, 0, end, ("#GR", "#PY"), (None, None), (3, 3)),
                        (11, None, end + 2 * SECOND, ("#GR", "#PY"), ("lose", "win"), (4, 5)),
                        (12, 0, end, ("#CU", "#PY"), (None, None), (1, 1)),
                        (13, HOUR, end + SECOND, ("#CU", "#PY"), (None, None), (1, 1)),
                        (14, None, end + 2 * SECOND, ("#CU", "#PY"), ("tie", "tie"), (2, 2)),
                        (15, None, end + 2 * SECOND, ("#LQ", "#PY"), ("win", "lose"), (6, 1)))
                for war_id, preparation, ends, clans, results, stars in wars:
                    c.execute("INSERT INTO wars(id,type,preparation_at,start_at,end_at,state,first_seen_at,last_seen_at,"
                              "roster_complete,attacks_complete,result_complete,content_hash,field_presence_json,"
                              "source_run_id) VALUES (?,'UNKNOWN',?,?,?,'warEnded',1,1,1,1,1,'old','[]',1)",
                              (war_id, preparation, None if preparation is None else preparation + 23 * HOUR, ends))
                    for side in (0, 1):
                        c.execute("INSERT INTO war_sides(war_id,side,clan_tag,stars,xp_earned,result,extra_json,source_run_id) "
                                  "VALUES (?,?,?,?,?,?,'{}',1)", (war_id, side, clans[side], stars[side],
                                                                  None if preparation is not None else 50, results[side]))
                    pair = ":".join(clans)
                    for kind in ("end", "war_log" if preparation is None else "preparation"):
                        c.execute("INSERT INTO war_source_keys VALUES (?,?,?)",
                                  (kind, f"{pair}:{ends if kind != 'preparation' else preparation}", war_id))
                c.execute("INSERT INTO war_collection_state(source_kind,scope_id,next_due_at,war_id) VALUES ('current_war','#PY',1,11)")
                c.commit()

        await asyncio.to_thread(old_record)
        upgraded = FamilyStore(path)
        await upgraded.initialize()

        def read(c):
            return ({row["id"]: (row["type"], row["type_basis"], row["end_at"])
                     for row in c.execute("SELECT * FROM wars")},
                    [tuple(row) for row in c.execute("SELECT stars,xp_earned,result FROM war_sides WHERE war_id=10 ORDER BY side")],
                    sorted(tuple(row) for row in c.execute("SELECT source_kind,war_id FROM war_source_keys WHERE source_key LIKE '#GR:#PY:%'")),
                    c.execute("SELECT war_id FROM war_collection_state").fetchone()[0],
                    c.execute("SELECT COUNT(*) FROM war_sides WHERE war_id=11").fetchone()[0],
                    c.execute("PRAGMA foreign_key_check").fetchall())
        wars, sides, keys, current, orphaned, foreign = await upgraded.call(read)
        self.assertEqual(wars, {10: ("REG", "war_log", end), 12: ("REG", "times", end), 13: ("REG", "times", end + SECOND),
                                14: ("REG", "war_log", end + 2 * SECOND), 15: ("REG", "war_log", end + 2 * SECOND)})
        # Own counts stay; only what the current-war copy lacked comes from the log.
        self.assertEqual(sides, [(3, 50, "lose"), (3, 50, "win")])
        self.assertEqual(keys, [("end", 10), ("end", 10), ("preparation", 10), ("war_log", 10)])
        self.assertEqual((current, orphaned, foreign), (10, 0, []))


class LegacyWarTests(WarFixture, unittest.IsolatedAsyncioTestCase):
    async def test_an_old_id_is_accepted_once_and_conflicts_are_refused(self):
        await self.record(war("warEnded"), at=END + 90 * SECOND)
        await self.record(war("warEnded", clans=("#PY", "#CU"), end="20261030T180000.000Z"), at=END + 90 * SECOND)
        first, second = [r["id"] for r in await self.rows("SELECT id FROM wars ORDER BY id")]
        evidence = {"label": "REG", "capture_state": "warEnded", "captured_at": END, "at": END + 600 * SECOND}

        def mapping(*args, **kwargs):
            def write(c):
                result = map_legacy_war(c, *args, **kwargs)
                c.commit()
                return result
            return self.store.call(write)
        old_id = "REG:#PY:20261028T180000.000Z:#GR"
        self.assertTrue(await mapping("clan_health", old_id, "PY", first, **evidence))
        self.assertFalse(await mapping("clan_health", old_id, "PY", first, **evidence))
        stored = await self.rows("SELECT * FROM war_legacy_ids")
        for war_id, changes in ((second, {}), (first, {"label": "CWL"}), (first, {"capture_state": "inWar"}),
                                (first, {"captured_at": END + 1})):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, "legacy_war_conflict"):
                    await mapping("clan_health", old_id, "PY", war_id, **{**evidence, **changes})
        self.assertEqual(stored, await self.rows("SELECT * FROM war_legacy_ids"))
        # A league war ID is shared by every family clan that fought it.
        self.assertTrue(await mapping("clan_health", old_id, "LQ", first, **evidence))
        self.assertTrue(await mapping("war_cache", "20261026T180000.000Z-20261028T180000.000Z-#GR", "PY", first,
                                      capture_state="warEnded", at=END))
        for args in (("old_store", old_id, "PY", first), ("clan_health", "", "PY", first),
                     ("clan_health", "other", "PY", 999)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                await mapping(*args, at=END)
        self.assertEqual(len(await self.rows("SELECT * FROM war_legacy_ids")), 3)
        self.assertEqual(await self.rows("PRAGMA foreign_key_check"), [])


class WarCoverageTests(WarFixture, unittest.IsolatedAsyncioTestCase):
    async def signal(self, kind, scope, at):
        return await self.store.call(war_signal, kind, scope, at)

    async def test_source_freshness_failures_privacy_and_staleness(self):
        self.assertEqual((await self.signal("current_war", "#PY", self.at))["status"], "not_tracked")
        await self.record(war())
        fresh = await self.signal("current_war", "#PY", self.at)
        self.assertEqual((fresh["status"], fresh["last_good_at"], fresh["source_complete"], fresh["observed"]["state"]),
                         ("observed", self.at, True, "inWar"))
        self.assertTrue(envelope([fresh], at=self.at)["complete"])
        await self.record(None, status=503, at=self.at + 300 * SECOND)
        failing = await self.signal("current_war", "#PY", self.at + 300 * SECOND)
        self.assertEqual((failing["status"], failing["last_good_at"], [f["http_status"] for f in failing["failures"]]),
                         ("partial", self.at, [503]))
        await self.record({"reason": "accessDenied"}, status=403, at=self.at + 600 * SECOND)
        self.assertEqual((await self.signal("current_war", "#PY", self.at + 600 * SECOND))["status"], "private")
        await self.record(war(), at=self.at + 900 * SECOND)
        due = (await self.rows("SELECT next_due_at FROM war_collection_state WHERE source_kind='current_war' AND scope_id='#PY'"))[0]["next_due_at"]
        late = due + WAR_CADENCES["current_war"] + SECOND
        stale = await self.signal("current_war", "#PY", late)
        self.assertEqual((stale["status"], stale["source_complete"], stale["gaps"][0]["cause"]), ("stale", False, "overdue"))
        self.assertFalse(envelope([stale], at=late)["complete"])
        # A source that never succeeded is unavailable, not stale.
        await self.record({"reason": "accessDenied"}, kind="war_log", status=403, at=self.at)
        await self.record(None, scope="#LQ", status=503)
        self.assertEqual((await self.signal("war_log", "#PY", self.at))["status"], "private")
        self.assertEqual((await self.signal("current_war", "#LQ", self.at))["status"], "unavailable")

    async def test_log_limits_and_identity_conflicts(self):
        # Two live wars share a clan pair and end, so the log entry stays apart.
        await self.record(war())
        other = war()
        other["preparationStartTime"] = "20261026T170000.000Z"
        await self.record(other)

        async def limits():
            return (await self.signal("war_log", "#PY", self.at))["log_limits"]
        await self.record(war_log(), kind="war_log")
        self.assertEqual(await limits(), {"history_complete": True, "api_history_limit": False, "backfill_pending": False})
        await self.record(war_log(war("warEnded", clans=("#PY", "#CU"))), kind="war_log")
        self.assertEqual(await limits(), {"history_complete": False, "api_history_limit": True, "backfill_pending": False})
        await self.record(war_log(after="next"), kind="war_log")
        self.assertEqual(await limits(), {"history_complete": False, "api_history_limit": False, "backfill_pending": True})
        conflicts = (await self.signal("current_war", "#PY", self.at))["identity_conflicts"]
        self.assertEqual([(c["clan_tags"], c["end_at"], len(c["war_ids"])) for c in conflicts],
                         [(["#GR", "#PY"], END, 3)])
        self.assertEqual((await self.signal("current_war", "#LQ", self.at))["identity_conflicts"], [])

    async def test_own_side_completeness_is_kept_apart_from_the_opponent(self):
        partial = war("warEnded")
        partial["opponent"].pop("members")
        await self.record(partial, at=END + 90 * SECOND)
        war_id = (await self.rows("SELECT id FROM wars"))[0]["id"]
        coverage = await self.store.call(war_side_coverage, war_id, "#PY")
        self.assertEqual((coverage["own"]["clan_tag"], coverage["own"]["lineup_complete"], coverage["own"]["attacks_complete"],
                          coverage["own"]["attacks_stored"]), ("#PY", True, True, 2))
        self.assertEqual((coverage["opponent"]["lineup_complete"], coverage["opponent"]["attacks_complete"],
                          coverage["opponent"]["attacks_stored"], coverage["opponent"]["attacks_reported"]), (False, False, 0, 2))
        # The result is final; the opponent's attack detail is still missing.
        self.assertEqual((coverage["type"], coverage["type_basis"], coverage["final"]), ("REG", "times", True))
        await self.record(war("warEnded"), at=END + 300 * SECOND)
        complete = await self.store.call(war_side_coverage, war_id, "#GR")
        self.assertEqual((complete["own"]["clan_tag"], complete["own"]["attacks_complete"],
                          complete["opponent"]["attacks_complete"], complete["final"]), ("#GR", True, True, True))
        self.assertIsNone(await self.store.call(war_side_coverage, war_id, "#LQ"))
        aggregate = war_log(war("warEnded", clans=("#PY", "#CU")))
        await self.record(aggregate, kind="war_log")
        logged = (await self.rows("SELECT id FROM wars WHERE preparation_at IS NULL"))[0]["id"]
        only_totals = await self.store.call(war_side_coverage, logged, "#PY")
        self.assertEqual((only_totals["own"]["members"], only_totals["own"]["lineup_complete"]), (0, False))
