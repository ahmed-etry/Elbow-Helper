"""Synthetic collection evidence and interval invariants, with real SQLite."""

import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest

from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store.clans import collect_clans
from elbow_helper.data.store.people import mirror_links, observe_discord
from elbow_helper.data.store.schema import MIGRATIONS, SCHEMA_VERSION
from elbow_helper.infrastructure.clash.client import ClashResponse
from elbow_helper.infrastructure.persistence.migrations import (
    SQLiteMigration, UnversionedSQLiteSchemaError, UnsupportedSQLiteVersionError, run_sqlite_migrations,
)


CLANS = (
    SimpleNamespace(code="ONE", tag="#PY", name="First clan"),
    SimpleNamespace(code="TWO", tag="#LQ", name="Second clan"),
)


def response(payload, status=200):
    return ClashResponse(status, payload, {}, 1, 2)


def player(tag="#P0", *, name="Player", role="member", **counters):
    return {"tag": tag, "name": name, "role": role, "townHallLevel": 16, **counters}


def clan_response(index, rows, *, declared=None, **profile):
    return response({"tag": CLANS[index].tag, "name": CLANS[index].name,
                     "members": len(rows) if declared is None else declared, "memberList": rows,
                     "clanLevel": 20, "clanPoints": 12345,
                     "badgeUrls": {"small": "https://example.test/badge"}, **profile})


def link(owner=100, **fields):
    return {"#P0": {"player_tag": "#P0", "discord_user_id": owner,
                    "player_name_last_seen": None, "last_seen_clan_tag": "", "last_seen_clan_code": None,
                    "last_seen_role": "member", **fields}}


def discord_member(member_id=100, *, name="Username", nick="Nickname", roles=None):
    return {"id": member_id, "username": name, "display_name": "Display", "nick": nick,
            "bot": False, "joined_at": 1, "roles": [10, 20] if roles is None else roles}


class FamilyStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "family.sqlite3"
        self.store = FamilyStore(self.path)
        await self.store.initialize()

    async def rows(self, sql, args=()):
        return await self.store.call(lambda connection: [dict(row) for row in connection.execute(sql, args)])

    async def clans(self, at, responses=None):
        specs = [(kind, clan.tag) for clan in CLANS for kind in ("clan_profile", "clan_roster")]
        run = await self.store.begin_run("clans", "family_clans", specs, at)
        for ordinal in range(4):
            await self.store.requested(run, ordinal)
        await self.store.call(collect_clans, run, CLANS, responses or [clan_response(0, [player()]), clan_response(1, [])], at)
        await self.store.finish(run)
        return run

    async def links(self, at, payload):
        run = await self.store.begin_run("links", "current_links", [("account_links", "current")], at)
        await self.store.requested(run, 0)
        await self.store.call(mirror_links, run, payload, at)
        await self.store.finish(run)
        return run

    async def discord(self, at, rows, **kwargs):
        run = await self.store.begin_run("discord", "guild", [("discord_scan", "guild")], at)
        await self.store.requested(run, 0)
        await self.store.call(observe_discord, run, rows, at, **kwargs)
        await self.store.finish(run)
        return run

    async def test_schema_pragmas_foreign_keys_and_backup_inventory(self):
        from elbow_helper.configuration.files import FAMILY_DATABASE, SQLITE_DATABASES
        flags = await self.store.call(lambda c: [c.execute(f"PRAGMA {key}").fetchone()[0]
                                                for key in ("user_version", "journal_mode", "foreign_keys")])
        self.assertEqual(flags, [SCHEMA_VERSION, "wal", 1])
        self.assertIn(FAMILY_DATABASE, SQLITE_DATABASES)
        self.assertEqual(FAMILY_DATABASE, Path("family/family.sqlite3"))
        tables = {row["name"] for row in await self.rows("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {
            "members", "member_aliases", "guild_membership", "member_roles", "accounts", "account_aliases",
            "account_links", "account_link_metadata", "clans", "clan_observations", "clan_counters",
            "clan_membership", "donation_snapshots", "collection_runs", "collection_items",
            "player_observations", "item_catalog", "player_items", "hero_equipment", "player_counters",
            "player_collection_state", "player_coverage",
            "wars", "war_source_keys", "war_sides", "war_members", "war_attacks", "cwl_seasons", "cwl_log_summaries",
            "cwl_clans", "cwl_players", "cwl_rounds", "cwl_season_sources", "war_collection_state", "api_records", "api_field_changes", "player_ranked_references",
            "family_api_state", "family_api_entities",
            "family_battles", "battle_strings", "battle_changes",
        })
        with self.assertRaises(sqlite3.IntegrityError):
            await self.store.call(lambda c: c.execute(
                "INSERT INTO account_link_metadata(account_id,basis,source_item_id) VALUES (999,'bad',999)"))

    async def test_future_migration_preserves_facts_and_rejects_downgrade(self):
        await self.links(100, link())

        def upgrade(connection):
            migration = SQLiteMigration(SCHEMA_VERSION + 1, "later subject", lambda c: c.execute("CREATE TABLE later_subject(id INTEGER PRIMARY KEY)"))
            return run_sqlite_migrations(connection, (*MIGRATIONS, migration), target_version=SCHEMA_VERSION + 1)

        self.assertEqual(await self.store.call(upgrade), (SCHEMA_VERSION + 1,))
        self.assertEqual(len(await self.rows("SELECT * FROM account_links")), 1)
        newer = FamilyStore(self.path)
        with self.assertRaises(UnsupportedSQLiteVersionError):
            await newer.initialize()

    async def test_unversioned_nonempty_schema_is_rejected_unchanged(self):
        other = self.path.parent / "unknown.sqlite3"
        with closing(sqlite3.connect(other)) as c, c:
            c.execute("CREATE TABLE unknown(value TEXT)")
            c.execute("INSERT INTO unknown VALUES ('saved')")
        original = other.read_bytes()
        with self.assertRaises(UnversionedSQLiteSchemaError):
            await FamilyStore(other).initialize()
        self.assertEqual(other.read_bytes(), original)

    async def test_failed_future_migration_rolls_back_schema_and_version(self):
        def upgrade(c):
            def fail(c):
                c.execute("CREATE TABLE not_committed(value INTEGER)")
                raise ValueError("synthetic failure")
            return run_sqlite_migrations(c, (*MIGRATIONS, SQLiteMigration(SCHEMA_VERSION + 1, "failed subject", fail)), target_version=SCHEMA_VERSION + 1)

        from elbow_helper.infrastructure.persistence.migrations import SQLiteMigrationError
        with self.assertRaises(SQLiteMigrationError):
            await self.store.call(upgrade)
        self.assertEqual(await self.rows("SELECT name FROM sqlite_master WHERE name='not_committed'"), [])
        self.assertEqual(await self.store.call(lambda c: c.execute("PRAGMA user_version").fetchone()[0]), SCHEMA_VERSION)

    async def test_restart_recovery_and_unchanged_roster_do_not_duplicate_open_intervals(self):
        await self.clans(100)
        pending = await self.store.begin_run("clans", "family_clans", [("clan_profile", "#PY"), ("clan_roster", "#PY")], 150)
        await self.store.requested(pending, 0)
        self.store = FamilyStore(self.path)
        await self.store.initialize()
        await self.clans(200)
        stays = await self.rows("SELECT * FROM clan_membership")
        self.assertEqual(len(stays), 1)
        self.assertEqual((stays[0]["from_at"], stays[0]["last_observed_at"]), (100, 200))
        old_run = (await self.rows("SELECT * FROM collection_runs WHERE id=?", (pending,)))[0]
        self.assertEqual((old_run["status"], old_run["attempted"], old_run["failed"], old_run["skipped"]), ("interrupted", 1, 1, 1))
        self.assertEqual(await self.rows("PRAGMA foreign_key_check"), [])

    async def test_role_change_and_complete_absence_have_observation_bounds(self):
        await self.clans(100)
        await self.clans(200, [clan_response(0, [player(role="admin")]), clan_response(1, [])])
        await self.clans(300, [clan_response(0, []), clan_response(1, [])])
        stays = await self.rows("SELECT * FROM clan_membership ORDER BY from_at")
        self.assertEqual([(r["from_at"], r["to_at"], r["clan_role"]) for r in stays], [(100, 200, "member"), (200, 300, "admin")])
        self.assertEqual((stays[1]["previous_good_at"], stays[1]["end_previous_good_at"]), (100, 200))
        self.assertEqual([r["end_basis"] for r in stays], ["role_change", "complete_roster_absence"])

    async def test_transfer_closes_old_location_even_when_old_clan_failed(self):
        await self.clans(100)
        await self.clans(200, [response(None, 503), clan_response(1, [player()])])
        stays = await self.rows("SELECT * FROM clan_membership ORDER BY from_at")
        self.assertEqual([(r["from_at"], r["to_at"]) for r in stays], [(100, 200), (200, None)])
        self.assertEqual(stays[0]["end_basis"], "observed_destination")
        self.assertEqual(stays[1]["previous_good_at"], 100)
        self.assertEqual((await self.rows("SELECT status FROM collection_runs ORDER BY id DESC LIMIT 1"))[0]["status"], "partial")

    async def test_partial_and_failed_rosters_never_close_stays_or_refresh_good_time(self):
        await self.clans(100)
        await self.clans(200, [clan_response(0, [], declared=1), clan_response(1, [])])
        await self.clans(3_600_000_300, [clan_response(0, [player(role="admin", donations=4)], declared=2), clan_response(1, [])])
        await self.clans(3_600_000_400, [response(None, 429), clan_response(1, [])])
        stays = await self.rows("SELECT * FROM clan_membership")
        self.assertEqual(len(stays), 1)
        self.assertEqual((stays[0]["to_at"], stays[0]["clan_role"], stays[0]["last_observed_at"]), (None, "member", 100))
        self.assertEqual([r["donations"] for r in await self.rows("SELECT * FROM donation_snapshots ORDER BY sampled_at")], [None, 4])

    async def test_cross_clan_conflict_retains_last_good_and_recovers_next_poll(self):
        await self.clans(100)
        await self.clans(200, [clan_response(0, [player()]), clan_response(1, [player()])])
        self.assertEqual((await self.rows("SELECT last_observed_at FROM clan_membership WHERE to_at IS NULL"))[0]["last_observed_at"], 100)
        self.assertEqual(len(await self.rows("SELECT * FROM collection_items WHERE error_code='roster_conflict'")), 2)
        await self.clans(300, [clan_response(0, []), clan_response(1, [player()])])
        self.assertEqual([(r["from_at"], r["to_at"]) for r in await self.rows("SELECT * FROM clan_membership ORDER BY from_at")], [(100, 300), (300, None)])

    async def test_conflicting_rosters_do_not_mislabel_an_unrelated_fetch_failure(self):
        clans = (*CLANS, SimpleNamespace(code="THREE", tag="#GJ", name="Third clan"))
        specs = [(kind, clan.tag) for clan in clans for kind in ("clan_profile", "clan_roster")]
        run = await self.store.begin_run("clans", "family_clans", specs, 100)
        for ordinal in range(len(specs)):
            await self.store.requested(run, ordinal)
        await self.store.call(collect_clans, run, clans,
                              [clan_response(0, [player()]), clan_response(1, [player()]), None], 100)
        await self.store.finish(run)
        failures = await self.rows("SELECT error_code FROM collection_items WHERE scope_id='#GJ'")
        self.assertEqual([row["error_code"] for row in failures], ["fetch_failed", "fetch_failed"])

    async def test_missing_duplicate_invalid_and_paginated_rosters_are_partial(self):
        await self.clans(100)
        bad_responses = [
            response({"tag": CLANS[0].tag, "name": CLANS[0].name, "members": 1}),
            clan_response(0, [player()], memberList=None),
            clan_response(0, [player(), player(tag="p0")]),
            clan_response(0, [{"tag": "#P0", "name": "Missing role"}]),
            clan_response(0, [player()], paging={"cursors": {"after": "next"}}),
            response({"tag": "#LQ", "name": "Wrong clan", "members": 1,
                      "memberList": [player(tag="#Q0", name="Wrong clan member", donations=99)]}),
            response([]),
        ]
        for at, profile in enumerate(bad_responses, 200):
            run = await self.clans(at, [profile, clan_response(1, [])])
            roster = (await self.rows("SELECT * FROM collection_items WHERE run_id=? AND ordinal=1", (run,)))[0]
            self.assertEqual(roster["status"], "failed")
        self.assertEqual((await self.rows("SELECT last_observed_at FROM clan_membership WHERE to_at IS NULL"))[0]["last_observed_at"], 100)
        self.assertEqual(await self.rows("SELECT * FROM accounts WHERE player_tag='#Q0'"), [])

    async def test_changed_names_and_metadata_hourly_counters_and_missing_fields(self):
        await self.clans(100, [clan_response(0, [player(donations=0)]), clan_response(1, [])])
        await self.clans(200, [clan_response(0, [player(name="New name", donationsReceived=3)], clanPoints=13000), clan_response(1, [])])
        await self.clans(3_600_000_100, [clan_response(0, [player(name="New name", donationsReceived=3)], requiredTrophies=5000), clan_response(1, [])])
        aliases = await self.rows("SELECT * FROM account_aliases ORDER BY from_at")
        self.assertEqual([(r["name"], r["to_at"]) for r in aliases], [("Player", 200), ("New name", None)])
        metadata = await self.rows("SELECT * FROM clan_observations WHERE clan_id=1 ORDER BY observed_at")
        self.assertEqual(len(metadata), 2)
        self.assertNotIn("memberList", json.loads(metadata[0]["metadata_json"]))
        self.assertNotIn("clanPoints", json.loads(metadata[0]["metadata_json"]))
        self.assertEqual(json.loads(metadata[1]["metadata_json"])["requiredTrophies"], 5000)
        self.assertEqual(len(await self.rows("SELECT * FROM clan_counters WHERE clan_id=1")), 2)
        samples = await self.rows("SELECT * FROM donation_snapshots ORDER BY sampled_at")
        self.assertEqual((samples[0]["donations"], samples[0]["received"]), (0, None))
        self.assertEqual((samples[1]["donations"], samples[1]["received"]), (None, 3))
        self.assertEqual(json.loads(samples[0]["field_presence_json"]), 1)

    async def test_links_mirror_reassignment_unlink_restart_and_unknown_metadata_age(self):
        await self.links(100, link())
        self.store = FamilyStore(self.path)
        await self.store.initialize()
        await self.links(150, link())
        await self.links(200, link(200))
        await self.links(300, {})
        links = await self.rows("SELECT * FROM account_links ORDER BY from_at")
        self.assertEqual([(r["from_at"], r["to_at"]) for r in links], [(100, 200), (200, 300)])
        self.assertEqual(links[1]["previous_good_at"], 150)
        metadata = (await self.rows("SELECT * FROM account_link_metadata"))[0]
        self.assertEqual((metadata["player_name_last_seen"], metadata["last_seen_clan_tag"], metadata["last_seen_clan_code"], metadata["observed_at"]), (None, "", None, None))
        self.assertEqual(await self.rows("SELECT * FROM clan_membership"), [])
        self.assertEqual(await self.rows("SELECT * FROM account_aliases"), [])

    async def test_invalid_link_snapshot_cannot_close_ownership(self):
        await self.links(100, link())
        bad = link()
        bad["#P0"]["discord_user_id"] = "invalid"
        run = await self.store.begin_run("links", "current_links", [("account_links", "current")], 200)
        await self.store.requested(run, 0)
        with self.assertRaises(ValueError):
            await self.store.call(mirror_links, run, bad, 200)
        await self.store.finish(run, error="invalid_link_snapshot")
        self.assertEqual((await self.rows("SELECT to_at FROM account_links"))[0]["to_at"], None)
        self.assertEqual((await self.rows("SELECT failed FROM collection_runs WHERE id=?", (run,)))[0]["failed"], 1)

    async def test_discord_join_role_and_name_change_leave_and_rejoin(self):
        await self.discord(100, [discord_member()], basis="gateway_join")
        await self.discord(200, [discord_member(name="Renamed", nick=None, roles=[20, 30])], basis="gateway_update")
        await self.discord(300, [], basis="gateway_leave", departed_id=100)
        await self.discord(400, [discord_member(roles=[])], basis="gateway_join")
        stays = await self.rows("SELECT * FROM guild_membership ORDER BY from_at")
        self.assertEqual([(r["from_at"], r["to_at"], r["discord_joined_at"]) for r in stays], [(100, 300, 1), (400, None, 1)])
        self.assertEqual(stays[1]["previous_good_at"], 300)
        roles = await self.rows("SELECT * FROM member_roles ORDER BY role_id")
        self.assertEqual([(r["role_id"], r["from_at"], r["to_at"]) for r in roles], [(10, 100, 200), (20, 100, 300), (30, 200, 300)])
        self.assertEqual(roles[2]["previous_good_at"], 100)
        self.assertEqual((await self.rows("SELECT presence_status FROM members"))[0]["presence_status"], "present")
        username_aliases = await self.rows("SELECT * FROM member_aliases WHERE kind='username' ORDER BY from_at")
        self.assertEqual([r["name"] for r in username_aliases], ["Username", "Renamed", "Username"])

    async def test_discord_partial_scan_preserves_absent_members_complete_scan_closes(self):
        await self.discord(100, [discord_member(), discord_member(200)], complete=True)
        await self.discord(200, [discord_member()], complete=False)
        self.assertEqual(len(await self.rows("SELECT * FROM guild_membership WHERE to_at IS NULL")), 2)
        await self.discord(300, [discord_member()], complete=True)
        self.assertEqual(len(await self.rows("SELECT * FROM guild_membership WHERE to_at IS NULL")), 1)
        absent = (await self.rows("SELECT * FROM guild_membership WHERE to_at IS NOT NULL"))[0]
        self.assertEqual((absent["to_at"], absent["end_previous_good_at"]), (300, 100))
        await self.discord(400, [], complete=True)
        self.assertEqual(len(await self.rows("SELECT * FROM member_roles WHERE to_at IS NULL")), 0)

    async def test_empty_ok_failed_run_counts_and_sanitized_item_evidence(self):
        await self.clans(100)
        await self.links(200, {})
        await self.clans(300, [response(None, 503), response(None, 403)])
        runs = await self.rows("SELECT * FROM collection_runs ORDER BY id")
        self.assertEqual([r["status"] for r in runs], ["ok", "empty", "failed"])
        for run in runs:
            self.assertEqual(run["expected"], sum(run[key] for key in ("succeeded", "empty", "failed", "skipped")))
            self.assertIsNotNone(run["finished_at"])
            self.assertIsNone(run["origin_attempts"])
            self.assertIsNone(run["cache_hits"])
        self.assertEqual((runs[0]["succeeded"], runs[0]["empty"]), (3, 1))
        self.assertEqual([r["http_status"] for r in await self.rows("SELECT * FROM collection_items WHERE run_id=3 ORDER BY ordinal")], [503, 503, 403, 403])
        zero = await self.store.begin_run("zero_work", "family_clans", [], 400)
        self.assertEqual(await self.store.finish(zero), {"status": "empty", "failed": 0, "succeeded": 0, "empty": 0})
        header = (await self.rows("SELECT * FROM collection_runs WHERE id=?", (zero,)))[0]
        self.assertEqual((header["expected"], header["attempted"], header["rows_written"]), (0, 0, 0))

    async def test_no_backdating_and_fact_outcome_rollback(self):
        await self.links(200, link())
        run = await self.store.begin_run("links", "current_links", [("account_links", "current")], 100)
        await self.store.requested(run, 0)
        with self.assertRaises(ValueError):
            await self.store.call(mirror_links, run, link(200), 100)
        await self.store.finish(run, error="out_of_order_observation")
        self.assertEqual([(r["from_at"], r["to_at"]) for r in await self.rows("SELECT * FROM account_links")], [(200, None)])
        self.assertEqual(len(await self.rows("SELECT * FROM members")), 1)

    async def test_due_time_survives_restart_and_does_not_catch_up(self):
        from elbow_helper.data.collectors.service import LINK_INTERVAL_SECONDS
        await self.links(100, {})
        self.store = FamilyStore(self.path)
        await self.store.initialize()
        self.assertFalse(await self.store.due("links", LINK_INTERVAL_SECONDS, 299_000_100))
        self.assertTrue(await self.store.due("links", LINK_INTERVAL_SECONDS, 300_000_100))

    async def test_disk_work_off_loop_and_writer_remains_serialized_after_cancellation(self):
        main_thread = threading.get_ident()
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        observed = []

        def first(c):
            observed.append(threading.get_ident())
            entered.set()
            release.wait(5)
            completed.set()

        task = asyncio.create_task(self.store.call(first))
        self.assertTrue(await asyncio.to_thread(entered.wait, 5))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        def second(c):
            self.assertTrue(completed.is_set())
            observed.append(threading.get_ident())

        next_task = asyncio.create_task(self.store.call(second))
        try:
            await asyncio.sleep(0.02)
            self.assertFalse(next_task.done())
        finally:
            release.set()
        await next_task
        self.assertNotIn(main_thread, observed)
