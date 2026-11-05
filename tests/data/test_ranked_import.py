"""Synthetic ranked-copy rules, shared startup rollback, and continuing watch facts."""

import asyncio
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import sqlite3
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.data.collectors.service import FamilyCollectors
from elbow_helper.data.store import FamilyStore, ranked_import, wars_import
from elbow_helper.data.store.api_fields import leaves
from elbow_helper.data.store.battles import identity, read_battle_fields, save_battle
from elbow_helper.data.store.battle_coverage import observe, watch_fact
from elbow_helper.data.store.legacy_links import ImportMismatch
from elbow_helper.data.store.link_state import receipt
from elbow_helper.data.store.people import mirror_links
from elbow_helper.data.store.projections import canonical_json
from elbow_helper.data.store.ranked_import import snapshot_ranked, timestamp
from elbow_helper.data.store.schema import MIGRATIONS, SCHEMA_VERSION
from elbow_helper.data.store.sources import record_source
from elbow_helper.infrastructure.persistence.migrations import SQLiteMigration, SQLiteMigrationError, run_sqlite_migrations

from .legacy_ranked_schema import create_schema
from .test_family_store import link, response
from .test_wars_import import CLANS, ImportFixture, old_id, old_lineup, old_war, END_S


START = datetime(2026, 10, 16, tzinfo=timezone.utc)
BASE = timestamp(START.isoformat())
SECOND = 1_000_000


def iso(offset):
    return (START + timedelta(seconds=offset)).isoformat(timespec="microseconds")


def battle(offset=-10, *, mode="ranked", attack=True, **extra):
    return {"battleTimestamp": (START + timedelta(seconds=offset)).strftime("%Y%m%dT%H%M%S.000Z"),
            "battleType": mode, "attack": attack, "opponentPlayerTag": "#Q0", "opponentName": "Opponent",
            "stars": 3, "destructionPercentage": 100, "opponentTownHallLevel": 16, "armyShareCode": None,
            "lootedResources": [{"name": "Gold", "amount": 123}], "future": {"null": None, "empty": []}, **extra}


class RankedFixture(ImportFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.ranked = self.path.parent / "ranked.sqlite3"
        with closing(sqlite3.connect(self.ranked)) as old, old:
            create_schema(old)
            old.execute("PRAGMA user_version=1")
        run = await self.store.begin_run("links", "current", [("account_links", "current")], BASE)
        await self.store.call(mirror_links, run, link(), BASE)
        await self.store.finish(run)
        self.snapshot()
        self.poll()

    def source(self):
        return closing(sqlite3.connect(self.ranked))

    def snapshot(self, *, tag="#P0", owner=100, offset=0, status="ok"):
        with self.source() as old, old:
            old.execute("INSERT INTO snapshots VALUES (?,?,?,NULL,'Player',?,'{}')", (tag, iso(offset), owner, status))

    def poll(self, offset=0, status="ok", entries=0, skipped=0):
        with self.source() as old, old:
            old.execute("INSERT INTO battle_log_polls VALUES (?,?,100,NULL,?,?,NULL,NULL,?,300)",
                        ("#P0", iso(offset), status, entries, skipped))

    def add_battle(self, raw=None, *, key="old-key", clan="#LQ"):
        raw = battle() if raw is None else raw
        with self.source() as old, old:
            old.execute("INSERT INTO ranked_battles VALUES (?,?,?,?,?,?,100,?,'Player',?,?,?)",
                        (key, "#P0", datetime.strptime(raw["battleTimestamp"], "%Y%m%dT%H%M%S.%fZ").replace(
                            tzinfo=timezone.utc).isoformat(timespec="microseconds"), raw["battleType"].upper(),
                         int(raw["attack"]), raw.get("opponentPlayerTag"), clan, iso(0), iso(10), canonical_json(raw)))
        return raw

    async def copy(self):
        return await self.store.call(wars_import.copy_at_startup, self.health, None, self.ranked, clans=CLANS)

    async def seed_battle(self, raw):
        def write(c):
            item = receipt(c, "battle_logs", "synthetic", BASE, 1)
            run = c.execute("SELECT run_id FROM collection_items WHERE id=?", (item,)).fetchone()[0]
            save_battle(c, "#P0", raw, run, BASE)
            c.commit()
        await self.store.call(write)

    def collectors(self):
        bot = SimpleNamespace(boot_complete=asyncio.Event(), guild_id=1, get_guild=lambda _: None)
        return FamilyCollectors(bot, self.store, SimpleNamespace(get=AsyncMock()), (),
                                old_wars=(self.health, None, self.ranked))

    async def live(self, raw, offset, status=200):
        run = await self.store.begin_run("battle_logs", "family_api", [("battle_log", "#P0")], BASE + offset * SECOND)
        await self.store.requested(run, 0)
        await self.store.call(record_source, run, 0, {"source_kind": "battle_log", "scope_id": "#P0"},
                              response(raw, status) if status else None, BASE + offset * SECOND)
        await self.store.finish(run)


class CopyTests(RankedFixture, unittest.IsolatedAsyncioTestCase):
    async def test_payloads_modes_directions_names_tails_and_first_seen_clan_survive(self):
        raws = [self.add_battle(), self.add_battle(battle(-11, mode="legend", attack=False), key="legend", clan=None),
                self.add_battle(battle(-12, opponentPlayerTag=None, opponentName="No tag"), key="name"),
                self.add_battle(battle(-13, opponentPlayerTag=None, opponentName=None), key="no-name")]
        with self.source() as old, old:
            old.execute("INSERT INTO profile_health VALUES ('#P0',1)")
        before_links = await self.rows("SELECT * FROM account_links")
        result = (await self.copy())["ranked"]["checks"]
        self.assertEqual(result["inserted"], {"battles": 4, "watch_facts": 1})
        self.assertEqual(result["before_first_poll"], 4)
        for raw in raws:
            actual = await self.store.call(read_battle_fields, canonical_json(["#P0", identity(raw)]))
            self.assertEqual(actual, {p: v for p, v, _ in leaves(raw)})
        provenance = [json.loads(r["legacy_first_seen_json"]) for r in await self.rows(
            "SELECT * FROM family_battles ORDER BY battle_at DESC")]
        self.assertEqual([r["clan_when_first_seen"] for r in provenance], ["#LQ", None, "#LQ", "#LQ"])
        self.assertEqual({r["basis"] for r in provenance}, {"clan_when_first_seen"})
        self.assertEqual(await self.rows("SELECT * FROM account_links"), before_links)
        self.assertEqual(await self.rows("SELECT * FROM clan_membership"), [])
        for table in ("player_counters", "player_observations"):
            self.assertEqual(await self.rows(f"SELECT * FROM {table}"), [])
        self.assertEqual(result["outcomes"]["snapshots"], {"excluded:profile_history_deferred": 1})
        self.assertEqual(result["outcomes"]["profile_health"], {"excluded:operational_state": 1})

    async def test_clean_empty_poll_and_failed_partial_spans_are_compacted(self):
        with self.source() as old, old:
            old.execute("DELETE FROM battle_log_polls")
        self.poll(0, "partial", 50, 49)
        self.poll(1, "unavailable", None)
        self.poll(2, "ok", 0)
        self.poll(3, "ok", 5)
        self.poll(4, "partial", 4, 1)
        result = (await self.copy())["ranked"]["checks"]
        row = (await self.rows("SELECT * FROM battle_log_coverage"))[0]
        self.assertEqual((row["first_attempt_at"], row["first_clean_at"], row["last_attempt_at"], row["last_clean_at"]),
                         (BASE, BASE + 2 * SECOND, BASE + 4 * SECOND, BASE + 3 * SECOND))
        self.assertEqual(tuple(row[k] for k in ("polls", "clean_polls", "partial_polls", "failed_polls", "empty_polls",
                                               "nonempty_polls", "entries", "skipped_entries")), (5, 2, 2, 1, 1, 3, 59, 50))
        spans = json.loads(row["failed_spans_json"])
        self.assertEqual((spans[0]["polls"], spans[0]["next_clean_at"], spans[1]["next_clean_at"]), (2, BASE + 2 * SECOND, None))
        self.assertEqual(result["outcomes"]["battle_log_polls"], {"compacted": 5})

    async def test_a_watcher_without_a_clean_poll_stays_unknown(self):
        with self.source() as old, old:
            old.execute("DELETE FROM battle_log_polls")
        self.poll(0, "unavailable", None)
        await self.copy()
        self.assertIsNone((await self.rows("SELECT first_clean_at FROM battle_log_coverage"))[0]["first_clean_at"])

    async def test_existing_battles_match_without_rewriting_facts_or_provenance_runs(self):
        raw = self.add_battle()
        await self.seed_battle(raw)
        before = (await self.rows("SELECT * FROM family_battles"))[0]
        checks = (await self.copy())["ranked"]["checks"]
        after = (await self.rows("SELECT * FROM family_battles"))[0]
        self.assertEqual({k: v for k, v in after.items() if k != "legacy_first_seen_json"},
                         {k: v for k, v in before.items() if k != "legacy_first_seen_json"})
        self.assertEqual(checks["outcomes"]["ranked_battles"], {"matched": 1, "imported": 0})
        self.assertEqual(await self.rows("SELECT * FROM battle_changes"), [])

    async def test_any_value_difference_aborts_every_pending_topic(self):
        raw = self.add_battle()
        await self.seed_battle({**raw, "stars": 2})
        with self.old() as old, old:
            old_war(old, old_id(END_S))
            old_lineup(old, old_id(END_S), [("#P0", 1)])
        before = await self.dump()
        with self.assertRaisesRegex(ImportMismatch, "ranked_battle_conflict"):
            await self.copy()
        self.assertEqual(await self.dump(), before)

    async def test_unknown_accounts_or_changed_ownership_abort_before_ranked_writes(self):
        for field, value in (("member_id", 200), ("player_tag", "#P2")):
            before = await self.dump()
            with self.source() as old, old:
                for table in ("snapshots", "battle_log_polls"):
                    old.execute(f"UPDATE {table} SET {field}=?", (value,))
            with self.assertRaisesRegex(ImportMismatch, "ranked_account_owner_mismatch"):
                await self.copy()
            self.assertEqual(await self.dump(), before)
            with self.source() as old, old:
                for table in ("snapshots", "battle_log_polls"):
                    old.execute(f"UPDATE {table} SET player_tag='#P0',member_id=100")

    async def test_bad_source_rows_abort_and_do_not_change_the_source(self):
        self.add_battle()
        mutations = [("ranked_battles", "battle_at", iso(-9)), ("ranked_battles", "attack", 0),
                     ("ranked_battles", "battle_type", "HOME_VILLAGE"), ("ranked_battles", "opponent_tag", "#Q2"),
                     ("ranked_battles", "last_observed_at", iso(-1)), ("ranked_battles", "payload_json", "{}"),
                     ("ranked_battles", "payload_json", "not json"), ("battle_log_polls", "status", "other"),
                     ("ranked_battles", "battle_key", ""), ("battle_log_polls", "status", "partial"),
                     ("battle_log_polls", "status", "unavailable"), ("battle_log_polls", "oldest_battle_at", iso(0)),
                     ("battle_log_polls", "skipped_entries", -1), ("battle_log_polls", "total_entries", -1),
                     ("battle_log_polls", "poll_interval_seconds", 0), ("snapshots", "observed_at", "2026-10-16"),
                     ("snapshots", "status", "unknown"), ("snapshots", "clan_tag", "bad")]
        for table, field, value in mutations:
            with self.subTest(table=table, field=field, value=value):
                with self.source() as old, old:
                    original = old.execute(f"SELECT {field} FROM {table}").fetchone()[0]
                    old.execute(f"UPDATE {table} SET {field}=?", (value,))
                before, source_hash = await self.dump(), sha256(self.ranked.read_bytes()).digest()
                with self.assertRaises(ImportMismatch):
                    await self.copy()
                self.assertEqual(await self.dump(), before)
                self.assertEqual(sha256(self.ranked.read_bytes()).digest(), source_hash)
                with self.source() as old, old:
                    old.execute(f"UPDATE {table} SET {field}=?", (original,))

    async def test_duplicate_record_identity_is_not_silently_deduplicated(self):
        self.add_battle(key="one")
        self.add_battle(key="two")
        with self.assertRaisesRegex(ImportMismatch, "duplicate_ranked_identity"):
            await self.copy()

    async def test_existing_first_seen_provenance_is_never_overwritten_on_conflict(self):
        raw = self.add_battle()
        await self.seed_battle(raw)
        await self.execute("UPDATE family_battles SET legacy_first_seen_json='{}'")
        before = await self.dump()
        with self.assertRaisesRegex(ImportMismatch, "ranked_clan_provenance_conflict"):
            await self.copy()
        self.assertEqual(await self.dump(), before)

    async def test_missing_written_battle_or_watch_fact_fails_the_final_readback(self):
        self.add_battle()
        before = await self.dump()
        for operation in ("save_battle", "save_watch"):
            with self.subTest(operation=operation), patch.object(ranked_import, operation), self.assertRaises(ImportMismatch):
                await self.copy()
            self.assertEqual(await self.dump(), before)

    async def test_wrong_version_schema_or_unreadable_source_aborts(self):
        before = await self.dump()
        with self.source() as old, old:
            old.execute("PRAGMA user_version=2")
        with self.assertRaisesRegex(ImportMismatch, "unsupported_ranked_version"):
            await self.copy()
        with self.source() as old, old:
            old.execute("PRAGMA user_version=1")
            old.execute("ALTER TABLE snapshots ADD COLUMN unexpected TEXT")
        with self.assertRaisesRegex(ImportMismatch, "legacy_schema_mismatch"):
            await self.copy()
        with self.assertRaisesRegex(ImportMismatch, "ranked_unreadable"):
            snapshot_ranked(self.ranked.parent / "missing.sqlite3")
        self.assertEqual(await self.dump(), before)

    async def test_snapshot_is_frozen_and_read_only_and_counts_are_checked(self):
        self.add_battle()
        source_hash = sha256(self.ranked.read_bytes()).digest()
        frozen = snapshot_ranked(self.ranked)
        self.assertEqual(source_hash, sha256(self.ranked.read_bytes()).digest())
        self.add_battle(battle(-20), key="later")
        bad = replace(frozen, counts={**frozen.counts, "ranked_battles": 2})
        before = await self.dump()
        with patch.object(ranked_import, "snapshot_ranked", return_value=bad), self.assertRaisesRegex(
                ImportMismatch, "source_rows_unaccounted"):
            await self.copy()
        self.assertEqual(await self.dump(), before)
        with patch.object(ranked_import, "snapshot_ranked", return_value=frozen):
            await self.copy()
        self.assertEqual(len(await self.rows("SELECT * FROM family_battles")), 1)

    async def test_second_start_reads_nothing_even_if_sources_changed_or_disappeared(self):
        self.add_battle()
        await self.copy()
        before = await self.dump()
        self.store = FamilyStore(self.path)
        with patch.object(ranked_import, "snapshot_ranked", side_effect=AssertionError("old ranked read")), \
                patch.object(wars_import, "snapshot_wars", side_effect=AssertionError("old wars read")):
            self.assertIsNone(await self.copy())
        self.assertEqual(await self.dump(), before)

    async def test_live_path_skips_completed_wars_and_runs_ranked_copy_on_a_worker(self):
        await self.store.call(wars_import.copy_at_startup, self.health, None, clans=CLANS)
        self.add_battle()
        original, threads = ranked_import.snapshot_ranked, []
        def snapshot(path):
            threads.append(threading.get_ident())
            return original(path)
        with patch.object(wars_import, "snapshot_wars", side_effect=AssertionError("war sources read")), \
                patch.object(ranked_import, "snapshot_ranked", snapshot):
            result = await self.copy()
        self.assertIsNone(result["regular"])
        self.assertIsNone(result["cwl"])
        self.assertTrue(result["ranked"]["imported"])
        self.assertNotEqual(threads, [threading.get_ident()])

    async def test_failed_final_check_rolls_everything_back_and_startup_continues(self):
        self.add_battle()
        before = await self.dump()
        owner = self.collectors()
        with patch.object(ranked_import, "verify_ranked", side_effect=ImportMismatch("forced_failure")), \
                self.assertLogs("elbow_helper.data.collectors.service", level="WARNING") as logs:
            await owner._copy_old_wars()
        self.assertIn("forced_failure", logs.output[0])
        self.assertEqual(await self.dump(), before)
        await owner._copy_old_wars()
        self.assertEqual(len(await self.rows("SELECT * FROM imports")), 4)

    async def test_battle_pass_waits_for_shared_copy_even_after_failure(self):
        owner = self.collectors()
        gate, order = asyncio.Event(), []
        async def copy():
            await gate.wait()
            order.append("copy failed")
        async def sources(owner, collector):
            if collector == "battle_logs":
                order.append("battle pass")
        owner._copy_old_wars = copy
        for name in ("collect_clans", "collect_players", "collect_wars", "scan_discord", "collect_links"):
            setattr(owner, name, AsyncMock())
        with patch("elbow_helper.data.collectors.service.due_sources", return_value=[{}]), \
                patch("elbow_helper.data.collectors.service.collect_sources", side_effect=sources):
            await owner.cog_load()
            owner.bot.boot_complete.set()
            await asyncio.sleep(0.05)
            self.assertEqual(order, [])
            gate.set()
            await asyncio.sleep(0.05)
            await owner.cog_unload()
        self.assertEqual(order[:2], ["copy failed", "battle pass"])


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
        await self.copy()
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
