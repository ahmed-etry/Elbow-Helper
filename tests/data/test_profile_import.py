"""Synthetic profile copy, sparse reads, coverage, migration and shared startup failures."""

import asyncio
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from elbow_helper.data.collectors.service import FamilyCollectors
from elbow_helper.data.reads import ProfileReads
from elbow_helper.data.reads.profiles import summary
from elbow_helper.data.store import FamilyStore, profile_import, wars_import
from elbow_helper.data.store.legacy_links import ImportMismatch
from elbow_helper.data.store.link_state import receipt
from elbow_helper.data.store.people import account
from elbow_helper.data.store.profile_import_plan import HEALTH, RANKED, TOTALS, snapshot_profiles
from elbow_helper.data.store.projections import canonical_json
from elbow_helper.data.store.schema import MIGRATIONS
from elbow_helper.features.clan_health.database.schema import create_schema as health_schema
from elbow_helper.infrastructure.persistence.migrations import SQLiteMigration, SQLiteMigrationError, run_sqlite_migrations

from .legacy_ranked_schema import create_schema as ranked_schema
from .test_ranked_import import BASE, SECOND, iso


class ProfileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.health, self.ranked = self.path / "health.db", self.path / "ranked.db"
        with closing(sqlite3.connect(self.health)) as c, c:
            health_schema(c)
            c.execute("PRAGMA user_version=2")
        with closing(sqlite3.connect(self.ranked)) as c, c:
            ranked_schema(c)
            c.execute("PRAGMA user_version=1")
        self.store = FamilyStore(self.path / "family.db")
        await self.store.initialize()
        self.reads = ProfileReads(self.store)

    def a(self, offset=0, tag="#P0", clan="ONE", **fields):
        values = {"townhall": 16, "trophies": 0, "donations": 10, "donations_received": 3,
                  "war_stars": 100, "capital_contrib": 50, "attack_wins": 2,
                  **{key: 1 for key in TOTALS}, **fields}
        with closing(sqlite3.connect(self.health)) as c, c:
            c.execute(f"INSERT INTO player_snapshots(captured_ts,clan_code,player_tag,player_name,{','.join(values)}) "
                      f"VALUES ({','.join('?' for _ in range(len(values) + 4))})",
                      (BASE // SECOND + offset, clan, tag, "Old name", *values.values()))

    def b(self, offset=10, tag="#P0", status="ok", **fields):
        values = {"trophies": 20, "bestTrophies": 200, "attackWins": 5, "defenseWins": 1, **fields} if status == "ok" else fields
        with closing(sqlite3.connect(self.ranked)) as c, c:
            c.execute("INSERT INTO snapshots VALUES (?,?,100,NULL,'Old name',?,?)",
                      (tag, iso(offset), status, canonical_json(values)))

    async def copy(self):
        def operation(c):
            frozen = profile_import.snapshot_for_record(c, self.health, self.ranked)
            with c:
                return profile_import.write_profiles(c, frozen)
        return await self.store.call(operation)

    async def rows(self, sql, args=()):
        return await self.store.call(lambda c: [dict(r) for r in c.execute(sql, args)])

    async def dump(self):
        return await self.store.call(lambda c: tuple(c.iterdump()))

    async def cutoff(self, offset=100, tag="#P0", table="player_observations"):
        def write(c):
            with c:
                identifier = account(c, tag, BASE + 90 * SECOND)
                item = receipt(c, "players", "synthetic", BASE + 100 * SECOND, 1)
                run = c.execute("SELECT run_id FROM collection_items WHERE id=?", (item,)).fetchone()[0]
                stamp = BASE + offset * SECOND
                if table == "player_observations":
                    c.execute("INSERT INTO player_observations VALUES (?,'townHallLevel',?,'17',NULL,?)", (identifier, stamp, run))
                elif table == "player_items":
                    c.execute("INSERT INTO item_catalog VALUES (1,'heroes','home','Hero',NULL,'hero','{}','')")
                    c.execute("INSERT INTO player_items VALUES (?,1,?,1,10,'{}',1,NULL,?)", (identifier, stamp, run))
                else:
                    c.execute("INSERT INTO player_counters VALUES (?,?,999,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,"
                              "NULL,'{}','{}','{\"fields\":1}','{}',?)", (identifier, stamp, run))
        await self.store.call(write)

    async def test_values_zero_donations_resets_totals_and_no_guessed_identity(self):
        self.a()
        self.a(1)
        self.a(2, donations=2, donations_received=0, hero_sum=4, games_total=900)
        result = (await self.copy())["checks"]
        self.assertEqual(result["outcomes"][HEALTH + ".player_snapshots"], {"copied": 2, "skipped:unchanged": 1})
        self.assertEqual(result["created_accounts"], 1)
        counters = await self.rows("SELECT * FROM player_counters ORDER BY sampled_at")
        self.assertEqual(len(counters), 2)
        self.assertEqual(counters[0]["trophies"], 0)
        self.assertEqual(json.loads(counters[1]["reset_context_json"])["decreased_fields"], ["donations", "donationsReceived"])
        self.assertNotIn("clan", json.loads(counters[0]["reset_context_json"]))
        self.assertEqual(len(await self.rows("SELECT * FROM old_profile_totals")), 2)
        for table in ("account_links", "account_aliases", "clan_membership", "members", "item_catalog", "player_items"):
            self.assertEqual(await self.rows(f"SELECT * FROM {table}"), [])
        self.assertEqual((await self.rows("SELECT * FROM accounts"))[0]["first_observed_at"], None)
        self.assertEqual((await self.reads.snapshot("#P0", as_of=BASE + 2 * SECOND))["old_totals"]["games_total"]["value"], 900)
        await self.store.call(lambda c: (account(c, "#P0", BASE + 100 * SECOND), c.commit()))
        self.assertEqual((await self.rows("SELECT * FROM accounts"))[0]["first_observed_at"], BASE + 100 * SECOND)

    async def test_ranked_wins_its_whole_good_span_and_health_resumes(self):
        self.a(0, trophies=10, attack_wins=2)
        self.b(10)
        self.a(20, trophies=999, attack_wins=999, donations=11)
        self.b(25, status="unavailable")
        self.b(30, trophies=22, attackWins=1)
        self.a(40, trophies=10, attack_wins=2)
        result = (await self.copy())["checks"]
        self.assertEqual(result["suppressed_by_ranked"], {"trophies": 1, "attackWins": 1})
        middle = await self.reads.snapshot("#P0", as_of=BASE + 20 * SECOND)
        self.assertEqual((middle["fields"]["trophies"], middle["field_observed_at"]["trophies"]), (20, BASE + 10 * SECOND))
        self.assertEqual(middle["fields"]["donations"], 11)
        self.assertNotIn("builderBaseTrophies", middle["fields"])
        self.assertEqual(middle["field_sources"]["trophies"], RANKED)
        final = await self.reads.snapshot("#P0", as_of=BASE + 40 * SECOND)
        self.assertEqual(final["fields"]["trophies"], 10)
        self.assertEqual(final["field_sources"]["trophies"], HEALTH)
        self.assertEqual(final["fields"]["bestTrophies"], 200)

    async def test_record_boundary_is_first_fact_in_any_profile_table(self):
        for table in ("player_counters", "player_items", "player_observations"):
            with self.subTest(table=table):
                # A distinct account avoids sharing a cutoff between table cases.
                tag = {"player_counters": "#P0", "player_items": "#P2", "player_observations": "#P8"}[table]
                await self.cutoff(100, tag, table)
                self.a(99, tag)
                self.a(100, tag, trophies=5)
                self.a(101, tag, trophies=6)
        before = await self.rows("SELECT * FROM accounts")
        checks = (await self.copy())["checks"]
        self.assertEqual(checks["outcomes"][HEALTH + ".player_snapshots"]["skipped:record_history"], 6)
        self.assertEqual(await self.rows("SELECT * FROM accounts"), before)
        rows = await self.rows("SELECT p.* FROM player_counters p JOIN collection_runs r ON r.id=p.source_run_id "
                               "WHERE r.collector='profile_import'")
        self.assertTrue(all(r["sampled_at"] == BASE + 99 * SECOND for r in rows))
        # The dedicated history read can go before the collector's identity date.
        self.assertEqual((await self.reads.snapshot("#P2", as_of=BASE + 99 * SECOND))["fields"]["trophies"], 0)

    async def test_null_skipping_duplicates_failed_coverage_gaps_and_sparse_totals(self):
        self.a()
        self.a(clan="TWO")
        self.a(3600, **{k: None for k in ("townhall", "war_stars", "capital_contrib", "attack_wins", *TOTALS)}, donations=12)
        self.a(14400, hero_sum=None, spell_sum=7)
        self.b(10, status="unavailable")
        self.b(20, status="not_found")
        self.b(30, leagueTier={"id": 5})
        self.b(40, leagueTier={"id": 5})
        checks = (await self.copy())["checks"]
        self.assertEqual(checks["outcomes"][HEALTH + ".player_snapshots"]["skipped:duplicate_capture"], 1)
        self.assertEqual(checks["outcomes"][RANKED + ".snapshots"]["skipped:unchanged"], 1)
        value = await self.reads.snapshot("#P0", as_of=BASE + 14400 * SECOND)
        self.assertEqual(value["old_totals"]["hero_sum"]["observed_at"], BASE)
        self.assertEqual(value["old_totals"]["spell_sum"]["value"], 7)
        facts = {row["origin"]: row for row in value["coverage"]["old_sources"]}
        self.assertEqual(facts[HEALTH]["partial_samples"], 1)
        self.assertEqual(len(facts[HEALTH]["gaps_json"]), 1)
        self.assertEqual(facts[RANKED]["failed_samples"], 2)
        self.assertEqual(facts[RANKED]["first_good_at"], BASE + 30 * SECOND)
        self.assertEqual(value["coverage"]["collector"]["status"], "not_tracked")
        self.assertFalse(value["coverage"]["continuous_state_known"])

    async def test_legend_split_and_missing_league_are_not_invented(self):
        self.b(0, legendStatistics={"legendTrophies": 5, "currentSeason": {"trophies": 10}, "bestSeason": {"id": "x"}},
               league={"id": 2})
        self.b(1, legendStatistics={"legendTrophies": 6, "currentSeason": {"trophies": 11}, "bestSeason": {"id": "x"}})
        await self.copy()
        values = await self.reads.snapshot("#P0", as_of=BASE + SECOND)
        self.assertEqual(values["fields"]["legendStatistics.fast"]["legendTrophies"], 6)
        self.assertEqual(values["fields"]["legendStatistics.bestSeason"], {"id": "x"})
        self.assertNotIn("leagueTier", values["fields"])
        self.assertEqual(values["field_observed_at"]["league"], BASE)
        history = await self.reads.history("#P0", after=BASE, before=BASE + SECOND, as_of=BASE + SECOND, limit=1)
        self.assertTrue(history["truncated"])
        self.assertEqual(history["next_offset"], 1)
        self.assertEqual(history["rows"][0]["sources"]["legendStatistics.fast"], RANKED)
        with self.assertRaises(ValueError):
            await self.reads.history("#P0", after=2, before=1)

    async def test_conflicting_member_counters_keep_both_values_without_a_winner(self):
        self.a(donations=10, trophies=0)
        self.a(clan="TWO", donations=20, trophies=5)
        self.a(1, donations=15)
        checks = (await self.copy())["checks"]
        self.assertEqual(checks["outcomes"][HEALTH + ".player_snapshots"]["skipped:conflict_evidence_merged"], 1)
        result = await self.reads.snapshot("#P0", as_of=BASE)
        self.assertIsNone(result["fields"]["donations"])
        self.assertEqual(result["field_conflicts"]["donations"]["values"], [10, 20])
        self.assertEqual(result["field_conflicts"]["trophies"]["values"], [0, 5])
        next_sample = await self.reads.snapshot("#P0", as_of=BASE + SECOND)
        self.assertEqual(next_sample["fields"]["donations"], 15)
        self.assertEqual(next_sample["field_conflicts"], {})
        history = await self.reads.history("#P0", after=BASE, before=BASE + SECOND, as_of=BASE + SECOND)
        first, last = [r for r in history["rows"] if r["kind"] == "counters"]
        self.assertEqual(first["conflicts"]["donations"], [10, 20])
        self.assertEqual(last["reset_context"]["previous_conflicts"]["donations"], [10, 20])
        self.assertNotIn("donations", last["reset_context"].get("decreased_fields", []))

    async def test_ranked_authority_also_resolves_conflicting_health_trophies(self):
        self.b(0)
        self.b(20)
        self.a(10, trophies=1)
        self.a(10, clan="TWO", trophies=2)
        await self.copy()
        result = await self.reads.snapshot("#P0", as_of=BASE + 10 * SECOND)
        self.assertEqual(result["fields"]["trophies"], 20)
        self.assertEqual(result["field_conflicts"], {})

    async def test_bad_rows_and_conflicting_duplicates_abort_without_source_writes(self):
        self.a()
        self.b()
        for table, column, value in (("player_snapshots", "hero_sum", -1), ("player_snapshots", "player_tag", "bad"),
                                      ("snapshots", "fields_json", "[]"), ("snapshots", "fields_json", "{"),
                                      ("snapshots", "fields_json", '{"attackWins":true}'),
                                      ("snapshots", "fields_json", '{"unknown":1}'),
                                      ("snapshots", "status", "other"), ("snapshots", "observed_at", "bad")):
            path = self.health if table == "player_snapshots" else self.ranked
            with self.subTest(column=column, value=value):
                with closing(sqlite3.connect(path)) as c, c:
                    original = c.execute(f"SELECT {column} FROM {table}").fetchone()[0]
                    c.execute(f"UPDATE {table} SET {column}=?", (value,))
                hashes = [sha256(p.read_bytes()).digest() for p in (self.health, self.ranked)]
                before = await self.dump()
                with self.assertRaises(ImportMismatch):
                    await self.copy()
                self.assertEqual(await self.dump(), before)
                self.assertEqual(hashes, [sha256(p.read_bytes()).digest() for p in (self.health, self.ranked)])
                with closing(sqlite3.connect(path)) as c, c:
                    c.execute(f"UPDATE {table} SET {column}=?", (original,))
        self.a(clan="TWO", hero_sum=99)
        with self.assertRaisesRegex(ImportMismatch, "conflicting_profile_duplicate"):
            await self.copy()

    async def test_frozen_snapshot_ledger_validation_and_failure_roll_back_all_topics(self):
        self.a()
        frozen = snapshot_profiles(self.health, self.ranked, {})
        self.a(1, trophies=1)
        with patch.object(profile_import, "snapshot_profiles", return_value=frozen):
            checks = (await self.copy())["checks"]
        self.assertEqual(checks["source_rows"][HEALTH + ".player_snapshots"], 1)
        # A new store exercises all pending topics, then fails after profile writes.
        self.store = FamilyStore(self.path / "failure.db")
        await self.store.initialize()
        before = await self.dump()
        owner = FamilyCollectors(SimpleNamespace(boot_complete=asyncio.Event()), self.store, None, (),
                                 old_wars=(self.health, None, self.ranked))
        with patch.object(profile_import, "verify_profiles", side_effect=ImportMismatch("forced_after_profiles")), \
                self.assertLogs("elbow_helper.data.collectors.service", level="WARNING"):
            await owner._copy_old_wars()
        self.assertEqual(await self.dump(), before)
        bad = replace(frozen, counts={HEALTH + ".player_snapshots": 999})
        with patch.object(profile_import, "snapshot_profiles", return_value=bad), self.assertRaises(ImportMismatch):
            await self.copy()
        self.assertEqual(await self.dump(), before)

    async def test_shared_startup_second_start_and_sparse_existing_summary(self):
        self.a()
        await self.cutoff()
        result = await self.store.call(wars_import.copy_at_startup, self.health, None, self.ranked, clans={})
        self.assertTrue(result["profiles"]["imported"])
        before = await self.dump()
        with patch.object(profile_import, "snapshot_for_record", side_effect=AssertionError("source read")):
            self.assertIsNone(await self.store.call(wars_import.copy_at_startup, self.health, None, self.ranked))
        self.assertEqual(await self.dump(), before)
        result = await self.store.call(summary, "#P0", BASE + 100 * SECOND)
        self.assertEqual(result["fields"]["trophies"], 0)
        self.assertEqual(result["field_sources"]["trophies"], HEALTH)

    async def test_snapshot_runs_on_worker_and_newer_or_unreadable_sources_fail(self):
        main = threading.get_ident()
        original = profile_import.snapshot_profiles
        def wrapped(*args):
            self.assertNotEqual(threading.get_ident(), main)
            return original(*args)
        with patch.object(profile_import, "snapshot_profiles", side_effect=wrapped):
            await self.copy()
        with closing(sqlite3.connect(self.ranked)) as c:
            c.execute("PRAGMA user_version=99")
        with self.assertRaisesRegex(ImportMismatch, "unsupported_profile_source_version"):
            snapshot_profiles(self.health, self.ranked, {})
        with self.assertRaisesRegex(ImportMismatch, "profile_source_unreadable"):
            snapshot_profiles(self.health, self.path / "missing", {})

    async def test_all_failed_source_keeps_coverage_without_profile_values(self):
        self.b(0, status="not_found")
        self.b(1, status="unavailable")
        await self.copy()
        result = await self.reads.snapshot("#P0", as_of=BASE + SECOND)
        self.assertEqual(result["fields"], {})
        fact = result["coverage"]["old_sources"][0]
        self.assertIsNone(fact["first_good_at"])
        self.assertEqual(fact["failed_samples"], 2)
        self.assertEqual(result["first_observed_at"], None)

    async def test_future_bad_shapes_and_failed_payloads_abort(self):
        self.b(0)
        with self.assertRaisesRegex(ImportMismatch, "future_profile_sample"):
            snapshot_profiles(self.health, self.ranked, {}, BASE - 1)
        with closing(sqlite3.connect(self.ranked)) as c, c:
            c.execute("UPDATE snapshots SET status='unavailable'")
        with self.assertRaisesRegex(ImportMismatch, "bad_profile_ranked_fields"):
            await self.copy()
        with closing(sqlite3.connect(self.ranked)) as c, c:
            c.execute("DELETE FROM snapshots")
            c.execute("ALTER TABLE snapshots ADD COLUMN unknown TEXT")
        with self.assertRaisesRegex(ImportMismatch, "legacy_schema_mismatch"):
            await self.copy()

    async def test_changed_readback_is_rejected_and_everything_rolls_back(self):
        self.a()
        original = profile_import.verify_profiles
        def changed(c, *args):
            c.execute("UPDATE old_profile_totals SET hero_sum=99")
            return original(c, *args)
        before = await self.dump()
        with patch.object(profile_import, "verify_profiles", side_effect=changed), self.assertRaisesRegex(
                ImportMismatch, "profile_readback_mismatch"):
            await self.copy()
        self.assertEqual(await self.dump(), before)

    async def test_profile_schedule_waits_for_failed_copy_and_then_resumes(self):
        bot = SimpleNamespace(boot_complete=asyncio.Event())
        owner = FamilyCollectors(bot, self.store, None, (), old_wars=(self.health, None, self.ranked))
        gate, order = asyncio.Event(), []
        async def failed_copy():
            await gate.wait()
            order.append("copy failed")
        async def schedule(name, seconds, collect, *, startup=False, after=None):
            await bot.boot_complete.wait()
            if after is not None:
                await after
            order.append(name)
        with patch.object(owner, "_copy_old_wars", side_effect=failed_copy), patch.object(owner, "_schedule", side_effect=schedule):
            await owner.cog_load()
            bot.boot_complete.set()
            await asyncio.sleep(0)
            self.assertNotIn("players", order)
            gate.set()
            await asyncio.gather(*owner._tasks)
            self.assertLess(order.index("copy failed"), order.index("players"))
            await owner.cog_unload()

    async def test_profile_migration_failure_rolls_back_accounts_and_schema(self):
        path = self.path / "failed-migration.db"
        with closing(sqlite3.connect(path)) as c:
            run_sqlite_migrations(c, MIGRATIONS[:11], target_version=11)
            c.execute("INSERT INTO accounts VALUES (1,'#P0','Name',123,NULL,'observed')")
            c.commit()
            before = tuple(c.iterdump())
            with self.assertRaises(SQLiteMigrationError):
                def fail(connection):
                    MIGRATIONS[11].apply(connection)
                    raise ValueError("forced_migration_failure")
                run_sqlite_migrations(c, (*MIGRATIONS[:11], SQLiteMigration(12, "failure", fail)), target_version=12)
            self.assertEqual(tuple(c.iterdump()), before)

    async def test_accounts_migration_preserves_dates_and_references(self):
        path = self.path / "v11.db"
        with closing(sqlite3.connect(path)) as c:
            c.execute("PRAGMA foreign_keys=ON")
            run_sqlite_migrations(c, MIGRATIONS[:11], target_version=11)
            c.execute("INSERT INTO accounts VALUES (1,'#P0','Name',123,NULL,'observed')")
            c.commit()
        store = FamilyStore(path)
        await store.initialize()
        rows = await store.call(lambda c: [tuple(r) for r in c.execute("SELECT * FROM accounts")])
        self.assertEqual(rows, [(1, "#P0", "Name", 123, None, "observed")])
