"""Ongoing battle watches, failure recovery and family-record upgrades."""

from contextlib import closing
import json
import sqlite3
import unittest
from unittest.mock import patch

from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store.battle_coverage import observe, watch_fact
from elbow_helper.data.store.schema import MIGRATIONS, SCHEMA_VERSION
from elbow_helper.infrastructure.persistence.migrations import SQLiteMigration, SQLiteMigrationError, run_sqlite_migrations
from .record_fixtures import BASE, SECOND, RankedFixture, battle


class WatchTests(RankedFixture, unittest.IsolatedAsyncioTestCase):
    async def test_full_nonoverlapping_window_records_a_gap_without_erasing_battles(self):
        await self.live({"items": [battle(-30)]}, 100)
        await self.live({"items": [battle(-1000 - i) for i in range(50)]}, 101)
        row = (await self.rows("SELECT * FROM battle_log_coverage"))[0]
        self.assertEqual((row["clean_polls"], row["failed_polls"]), (1, 1))
        self.assertEqual(json.loads(row["failed_spans_json"])[0]["reasons"], {"battle_window_gap": 1})
        self.assertEqual(len(await self.rows("SELECT * FROM family_battles")), 51)

    async def test_live_clean_empty_failures_invalid_payload_and_recovery_use_same_fact(self):
        await self.live({"items": []}, 100)
        await self.live(None, 101, status=0)
        await self.live({"items": [{"attack": True}]}, 102)
        await self.live({"items": [battle(-30, opponentPlayerTag=None)]}, 103)
        row = (await self.rows("SELECT * FROM battle_log_coverage"))[0]
        self.assertEqual(row["origin"], "record")
        self.assertEqual((row["polls"], row["clean_polls"], row["failed_polls"], row["empty_polls"], row["nonempty_polls"]),
                         (4, 2, 2, 1, 1))
        span = json.loads(row["failed_spans_json"])[0]
        self.assertEqual((span["from_at"], span["last_at"], span["next_clean_at"]),
                         (BASE + 101 * SECOND, BASE + 102 * SECOND, BASE + 103 * SECOND))
        await self.seed_history()
        self.assertEqual({r["origin"] for r in await self.rows("SELECT * FROM battle_log_coverage")}, {"record", "legacy_ranked"})

    async def test_repeated_500_and_interrupted_attempts_are_watch_failures(self):
        await self.live(None, 100, status=500)
        await self.live(None, 101, status=500)
        run = await self.store.begin_run("battle_logs", "family_api", [("battle_log", "#P0")], BASE + 102 * SECOND)
        await self.store.requested(run, 0)
        self.store = FamilyStore(self.path)
        await self.store.initialize()
        row = (await self.rows("SELECT * FROM battle_log_coverage"))[0]
        self.assertEqual((row["polls"], row["failed_polls"], row["first_clean_at"]), (3, 3, None))
        self.assertIn("interrupted", json.loads(row["failed_spans_json"])[0]["reasons"])

    async def test_watch_fact_refuses_time_to_run_backwards(self):
        fact = watch_fact()
        observe(fact, 2, "ok", 0)
        with self.assertRaisesRegex(ValueError, "out_of_order_watch"):
            observe(fact, 1, "ok", 0)

    async def test_version_ten_upgrade_is_additive_and_failed_upgrade_rolls_back(self):
        path = self.path.parent / "version10.sqlite3"
        with closing(sqlite3.connect(path)) as c:
            run_sqlite_migrations(c, MIGRATIONS[:10], target_version=10)
            c.execute("INSERT INTO accounts(player_tag,first_observed_at,tracking_status) VALUES ('#P0',1,'tracked')")
            c.commit()
        def fail(c):
            MIGRATIONS[10].apply(c)
            raise ValueError("synthetic_failure")
        with patch("elbow_helper.data.store.MIGRATIONS", (*MIGRATIONS[:10], SQLiteMigration(11, "failed", fail), *MIGRATIONS[11:])):
            with self.assertRaises(SQLiteMigrationError):
                await FamilyStore(path).initialize()
        with closing(sqlite3.connect(path)) as c:
            self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0], 10)
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='battle_log_coverage'").fetchone())
        store = FamilyStore(path)
        await store.initialize()
        self.assertEqual(await store.call(lambda c: c.execute("PRAGMA user_version").fetchone()[0]), SCHEMA_VERSION)
        self.assertEqual(await store.call(lambda c: c.execute("SELECT player_tag FROM accounts").fetchone()[0]), "#P0")
