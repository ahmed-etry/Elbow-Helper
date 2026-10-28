"""Offline, sanitized endpoint shapes and durable source collection contracts."""

import asyncio
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.data.collectors.service import FamilyCollectors
from elbow_helper.data.collectors.sources import collect_sources, path_for
from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store.api_fields import leaves, read_fields, unpack
from elbow_helper.data.store.people import mirror_links
from elbow_helper.data.store.projections import canonical_json
from elbow_helper.data.store.schema import MIGRATIONS, SCHEMA_VERSION
from elbow_helper.data.store.sources import (
    DAY, INTERVALS, KINDS, REFERENCES, RANKED_FINAL_GRACE, RANKED_WEEK,
    battle_key, due_sources, entity_scope, record_source,
)
from elbow_helper.infrastructure.persistence.migrations import SQLiteMigration, SQLiteMigrationError, run_sqlite_migrations
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction

from .test_family_store import CLANS, link, response


FIXTURES = Path(__file__).parents[1] / "fixtures" / "family_sources"
BASE = 1_793_000_000_000_000
COLLECTOR = {kind: collector for collector, kinds in KINDS.items() for kind in kinds}


def samples():
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(FIXTURES.glob("*.json"))]


def sample(kind):
    return deepcopy(next(row for row in samples() if row["kind"] == kind))


def key_for(kind, row):
    if kind == "battle_log":
        return battle_key(row)
    if kind == "league_history":
        return str(row["leagueSeasonId"])
    if kind in {"capital_raids", "goldpass"}:
        return row["startTime"]
    return canonical_json(row["id"])


class SourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "family.sqlite3"
        self.store = FamilyStore(self.path)
        await self.store.initialize()

    async def rows(self, sql, args=()):
        return await self.store.call(lambda c: [dict(row) for row in c.execute(sql, args)])

    async def record(self, data, at=BASE, *, status=200, current=True):
        kind, scope = data["kind"], data["scope"]
        run = await self.store.begin_run(COLLECTOR[kind], "family_api", [(kind, scope)], at)
        await self.store.requested(run, 0)
        await self.store.call(record_source, run, 0, {"source_kind": kind, "scope_id": scope, "current": current},
                              response(data["body"], status), at)
        return run, await self.store.finish(run)

    async def seed_account(self):
        run = await self.store.begin_run("links", "current", [("account_links", "current")], BASE)
        await self.store.call(mirror_links, run, link(), BASE)
        await self.store.finish(run)
        return run

    async def assert_leaves(self, data):
        kind, scope, body = (data[k] for k in ("kind", "scope", "body"))
        if kind == "ranked_group":
            parts = [(kind, scope, body)]
        elif kind == "goldpass":
            parts = [(kind, entity_scope(scope, key_for(kind, body)), body)]
        else:
            parts = [(kind, entity_scope(scope, key_for(kind, row)), row) for row in body["items"]]
            parts.append((kind + "_envelope", scope, {**body, "items": []}))
        for source, target, raw in parts:
            self.assertEqual(await self.store.call(read_fields, source, target), {p: v for p, v, _ in leaves(raw)})

    async def test_every_captured_leaf_survives_each_source_and_unchanged_repoll(self):
        for index, data in enumerate(samples()):
            with self.subTest(kind=data["kind"], scope=data["scope"]):
                _, result = await self.record(data, BASE + index * DAY)
                self.assertEqual(result["failed"], 0)
                await self.assert_leaves(data)
                history = await self.rows("SELECT * FROM api_field_changes")
                # Fixtures retain the captured paging shape. Repeating the head
                # is a new sweep, rather than reusing its next-page cursor.
                def reset(c):
                    with sqlite_transaction(c):
                        c.execute("UPDATE family_api_state SET checkpoint_json='{}'")
                await self.store.call(reset)
                await self.record(data, BASE + index * DAY + DAY)
                self.assertEqual(history, await self.rows("SELECT * FROM api_field_changes"))
        self.assertEqual(await self.rows("PRAGMA foreign_key_check"), [])

    async def test_battles_dedupe_across_rolling_windows_reordering_corrections_and_restart(self):
        data = sample("battle_log")
        data["body"]["items"] = data["body"]["items"][:3]
        run, _ = await self.record(data)
        self.assertEqual(len(await self.rows("SELECT * FROM family_battles")), 3)
        self.store = FamilyStore(self.path)
        await self.store.initialize()
        changed = deepcopy(data["body"]["items"][0])
        changed["battleTimestamp"] = "20261028T100000.000Z"
        data["body"]["items"] = [changed, *reversed(data["body"]["items"][:2])]
        await self.record(data, BASE + INTERVALS["battle_log"])
        self.assertEqual(len(await self.rows("SELECT * FROM family_battles")), 4)
        data["body"]["items"][0]["future"] = {"new": [None, True]}
        await self.record(data, BASE + 2 * INTERVALS["battle_log"])
        self.assertEqual(len(await self.rows("SELECT * FROM family_battles")), 4)
        await self.assert_leaves(data)
        self.assertTrue(any(row["first_run_id"] == run for row in await self.rows("SELECT * FROM family_battles")))

    async def test_battle_identity_excludes_type_and_includes_account_opponent_and_direction(self):
        data = sample("battle_log")
        data["body"]["items"] = data["body"]["items"][:1]
        await self.record(data)
        data["body"]["items"][0]["battleType"] = "future_type"
        await self.record(data, BASE + DAY)
        self.assertEqual(len(await self.rows("SELECT * FROM family_battles")), 1)
        await self.assert_leaves(data)
        del data["body"]["items"][0]["battleType"]
        await self.record(data, BASE + 2 * DAY)
        self.assertEqual(len(await self.rows("SELECT * FROM family_battles")), 1)
        data["scope"] = "#PPY"
        await self.record(data, BASE + 3 * DAY)
        data["body"]["items"][0]["attack"] = not data["body"]["items"][0]["attack"]
        await self.record(data, BASE + 4 * DAY)
        data["body"]["items"][0]["opponentPlayerTag"] = "#PPQ"
        await self.record(data, BASE + 5 * DAY)
        self.assertEqual(len(await self.rows("SELECT * FROM family_battles")), 4)

    async def test_compact_battles_normalize_resources_and_write_only_corrections(self):
        data = sample("battle_log")
        data["body"]["items"] = data["body"]["items"][:2]
        first_run, _ = await self.record(data)
        tables = ("family_battles", "battle_strings", "battle_changes")
        before = {table: await self.rows(f"SELECT * FROM {table}") for table in tables}
        await self.record(data, BASE + DAY)
        for table in tables:
            self.assertEqual(before[table], await self.rows(f"SELECT * FROM {table}"))
        self.assertEqual(await self.rows("SELECT * FROM api_records WHERE source_kind IN ('battle_log','battle_extra')"), [])
        resources = [row["name"] for battle in data["body"]["items"] for field in
                     ("lootedResources", "extraLootedResources", "availableLoot") for row in battle.get(field, [])]
        for name in resources:
            self.assertEqual(len(await self.rows("SELECT * FROM battle_strings WHERE value=?", (name,))), 1)
        old_stars = data["body"]["items"][0]["stars"]
        data["body"]["items"][0]["stars"] = (old_stars + 1) % 4
        correction_run, _ = await self.record(data, BASE + 2 * DAY)
        changes = await self.rows("SELECT * FROM battle_changes")
        self.assertEqual(len(changes), 1)
        self.assertEqual((changes[0]["source_run_id"], changes[0]["prior_run_id"]), (correction_run, first_run))
        from elbow_helper.data.store.battles import STATE
        self.assertEqual(unpack(changes[0]["prior_state_z"])[STATE.index("stars")], old_stars)
        await self.assert_leaves(data)
        self.assertEqual(await self.rows("PRAGMA foreign_key_check"), [])

    async def test_unusual_known_battle_fields_and_future_resource_fields_remain_lossless(self):
        data = sample("battle_log")
        data["body"]["items"] = data["body"]["items"][:1]
        row = data["body"]["items"][0]
        row.update(stars=None, battleTime="future duration", destructionPercentage=1.5)
        row["lootedResources"][0]["future"] = {"value": None}
        row["extraLootedResources"] = []
        row["availableLoot"].append(deepcopy(row["availableLoot"][0]))
        await self.record(data)
        await self.assert_leaves(data)
        row.pop("battleTime")
        row["lootedResources"][0].pop("future")
        row["stars"] = 3
        await self.record(data, BASE + DAY)
        await self.assert_leaves(data)

    async def test_hourly_battle_budget_survives_restart_and_old_short_due_time(self):
        await self.seed_account()
        data = sample("battle_log")
        data["scope"] = "#P0"
        await self.record(data)
        self.assertEqual(INTERVALS["battle_log"], 3600 * 1_000_000)
        def old_due(c):
            with sqlite_transaction(c):
                c.execute("UPDATE family_api_state SET next_due_at=? WHERE source_kind='battle_log'", (BASE + 300_000_000,))
        await self.store.call(old_due)
        self.store = FamilyStore(self.path)
        await self.store.initialize()
        self.assertEqual(await self.store.call(due_sources, "battle_logs", CLANS, BASE + 3599_000_000), [])
        self.assertEqual(len(await self.store.call(due_sources, "battle_logs", CLANS, BASE + 3600_000_000)), 1)

    async def test_ranked_six_hour_poll_and_final_read_do_not_need_profile_rollover(self):
        run = await self.seed_account()
        season = 1792990800
        scope = canonical_json(["#P0", "#PP2", season])
        final_due = season * 1_000_000 + RANKED_WEEK + RANKED_FINAL_GRACE
        at = final_due - 2 * 3600_000_000
        def seed(c):
            with sqlite_transaction(c):
                c.execute("INSERT INTO player_ranked_references VALUES (1,?, '#PP2',?,NULL,NULL,?)", (BASE, season, run))
        await self.store.call(seed)
        data = sample("ranked_group")
        data["scope"] = scope
        self.assertEqual(INTERVALS["ranked_group"], 6 * 3600_000_000)
        await self.record(data, at)
        self.assertEqual((await self.rows("SELECT next_due_at FROM family_api_state"))[0]["next_due_at"], final_due)
        self.assertEqual(await self.store.call(due_sources, "ranked_groups", CLANS, final_due - 1), [])
        jobs = await self.store.call(due_sources, "ranked_groups", CLANS, final_due)
        self.assertEqual(len(jobs), 1)
        self.assertFalse(jobs[0]["current"])
        await self.record(data, final_due, current=False)
        self.assertEqual(await self.store.call(due_sources, "ranked_groups", CLANS, final_due + DAY), [])

    async def test_ranked_history_keeps_only_changed_member_leaves(self):
        data = sample("ranked_group")
        await self.record(data)
        before = await self.rows("SELECT * FROM api_field_changes")
        data["body"]["members"].reverse()
        await self.record(data, BASE + DAY)
        self.assertEqual(before, await self.rows("SELECT * FROM api_field_changes"))
        data["body"]["members"][0]["leagueTrophies"] += 1
        await self.record(data, BASE + 2 * DAY)
        patch = (await self.rows("SELECT * FROM api_field_changes ORDER BY source_run_id DESC"))[0]
        self.assertEqual(len(unpack(patch["patch_z"])[0]), 1)
        self.assertEqual(unpack(patch["patch_z"])[1], [])

    async def test_finished_seasons_and_raid_weekends_are_unique_and_keep_updates(self):
        for kind in ("league_history", "capital_raids"):
            data = sample(kind)
            data["body"]["items"] = data["body"]["items"][:1]
            data["body"].pop("paging", None)
            if kind == "capital_raids":
                data["body"]["items"][0]["state"] = "ongoing"
            await self.record(data)
            data["body"]["items"][0]["future"] = {"field": 1}
            if kind == "capital_raids":
                data["body"]["items"][0]["state"] = "ended"
                data["body"]["items"][0]["attackLog"][0]["districts"][0]["attacks"][0]["future"] = 3
            await self.record(data, BASE + DAY)
            await self.assert_leaves(data)
            entities = await self.rows("SELECT * FROM family_api_entities WHERE source_kind=?", (kind,))
            self.assertEqual(len(entities), 1)
            self.assertIsNotNone(entities[0]["final_at"])
            history = await self.rows("SELECT * FROM api_field_changes")
            await self.record(data, BASE + 2 * DAY)
            self.assertEqual(history, await self.rows("SELECT * FROM api_field_changes"))

    async def test_full_nonoverlapping_battle_window_is_partial_but_keeps_positive_evidence(self):
        data = sample("battle_log")
        await self.record(data)
        for row in data["body"]["items"]:
            row["battleTimestamp"] = row["battleTimestamp"].replace("2026", "2027", 1)
        _, report = await self.record(data, BASE + DAY)
        self.assertEqual(report["failed"], 1)
        self.assertEqual((await self.rows("SELECT error_code FROM collection_items"))[0]["error_code"], "battle_window_gap")
        self.assertEqual(len(await self.rows("SELECT * FROM family_battles")), 100)
        _, report = await self.record(data, BASE + 2 * DAY)
        self.assertEqual(report["failed"], 0)

    async def test_empty_windows_never_erase_battles_seasons_or_raid_history(self):
        for kind in ("battle_log", "league_history", "capital_raids"):
            data = sample(kind)
            await self.record(data)
            entities = await self.rows("SELECT * FROM family_api_entities")
            battles = await self.rows("SELECT * FROM family_battles")
            data["body"] = {"items": []}
            _, result = await self.record(data, BASE + DAY)
            self.assertEqual(result["status"], "empty")
            self.assertEqual(entities, await self.rows("SELECT * FROM family_api_entities"))
            self.assertEqual(battles, await self.rows("SELECT * FROM family_battles"))

    async def test_future_fields_survive_in_rows_envelopes_and_nested_arrays(self):
        for index, data in enumerate(samples()):
            data["body"]["future"] = {"null": None, "array": [True, {}, []]}
            if "items" in data["body"]:
                data["body"]["items"][0]["future"] = {"leaf": [1, "one"]}
            elif data["kind"] == "ranked_group":
                data["body"]["members"][0]["future"] = {"leaf": 2}
            _, report = await self.record(data, BASE + index * DAY)
            self.assertEqual(report["failed"], 0)
            await self.assert_leaves(data)

    async def test_duplicate_identities_invalid_paging_and_json_are_atomic_failures(self):
        for kind in ("battle_log", "league_history", "capital_raids", "catalogue", "legend_seasons"):
            data = sample(kind)
            data["body"].pop("paging", None)
            await self.record(data)
            baseline = await self.rows("SELECT * FROM api_field_changes")
            duplicate = deepcopy(data)
            duplicate["body"]["items"].append(deepcopy(duplicate["body"]["items"][0]))
            malformed = deepcopy(data)
            malformed["body"]["paging"] = {"cursors": {"after": 123}}
            invalid_json = deepcopy(data)
            invalid_json["body"]["future"] = float("nan")
            for broken in (duplicate, malformed, invalid_json):
                _, report = await self.record(broken, BASE + DAY)
                self.assertEqual(report["failed"], 1)
                self.assertEqual(baseline, await self.rows("SELECT * FROM api_field_changes"))

    async def test_transport_and_invalid_identity_fail_without_losing_prior_facts(self):
        for index, data in enumerate(samples()):
            at = BASE + index * DAY
            await self.record(data, at)
            history = await self.rows("SELECT * FROM api_field_changes")
            for status in (403, 404, 429, 500, 503):
                _, result = await self.record(data, at + DAY, status=status)
                self.assertEqual(result["failed"], 1)
            invalid = deepcopy(data)
            if data["kind"] == "ranked_group":
                invalid["body"]["members"] = None
            elif data["kind"] == "goldpass":
                invalid["body"]["endTime"] = "not-a-time"
            else:
                invalid["body"]["items"][0] = {"future": "no identity"}
            _, result = await self.record(invalid, at + 2 * DAY)
            self.assertEqual(result["failed"], 1)
            self.assertEqual(history, await self.rows("SELECT * FROM api_field_changes"))

    async def test_ranked_old_500_is_terminal_but_current_and_other_errors_retry(self):
        data = sample("ranked_group")
        await self.record(data, status=500, current=False)
        state = (await self.rows("SELECT * FROM family_api_state"))[0]
        self.assertEqual((state["enabled"], state["last_status"], state["last_http_status"]), (0, "old_season_unavailable", 500))
        other = deepcopy(data)
        other["scope"] = '["#PP0","#PP9",1792990800]'
        await self.record(other, status=500, current=True)
        self.assertEqual((await self.rows("SELECT enabled FROM family_api_state WHERE scope_id=?", (other["scope"],)))[0]["enabled"], 1)
        other["scope"] = '["#PP0","#PP9",1792386000]'
        await self.record(other, status=503, current=False)
        self.assertEqual((await self.rows("SELECT enabled FROM family_api_state WHERE scope_id=?", (other["scope"],)))[0]["enabled"], 1)
        await self.record(other, BASE + DAY, current=False)
        self.assertEqual((await self.rows("SELECT enabled FROM family_api_state WHERE scope_id=?", (other["scope"],)))[0]["enabled"], 0)

    async def test_persisted_due_times_discovery_and_ranked_player_specific_scope(self):
        run = await self.seed_account()
        def references(c):
            with sqlite_transaction(c):
                c.execute("INSERT INTO player_ranked_references VALUES (1,?, '#PP2',1792990800,'#PP9',1792386000,?)", (BASE, run))
        await self.store.call(references)
        for collector in KINDS:
            jobs = await self.store.call(due_sources, collector, CLANS, BASE)
            self.assertTrue(jobs)
            if collector == "ranked_groups":
                self.assertEqual(sorted(j["current"] for j in jobs), [False, True])
                self.assertTrue(all("playerTag=%23P0" in path_for(j) for j in jobs))
            def postpone(c):
                with sqlite_transaction(c):
                    for job in jobs:
                        c.execute("UPDATE family_api_state SET next_due_at=? WHERE source_kind=? AND scope_id=?",
                                  (BASE + INTERVALS[job["source_kind"]], job["source_kind"], job["scope_id"]))
            await self.store.call(postpone)
            self.store = FamilyStore(self.path)
            await self.store.initialize()
            self.assertEqual(await self.store.call(due_sources, collector, CLANS, BASE + 1), [])
            self.assertTrue(await self.store.call(due_sources, collector, CLANS, BASE + DAY))
        self.assertEqual(len(REFERENCES), 9)

    async def test_cancelled_run_recovery_marks_only_running_source_and_leaves_pending_due(self):
        run = await self.store.begin_run("battle_logs", "family_api", [("battle_log", "#PP0"), ("battle_log", "#PP2")], BASE)
        await self.store.requested(run, 0)
        self.store = FamilyStore(self.path)
        await self.store.initialize()
        row = (await self.rows("SELECT * FROM collection_runs"))[0]
        self.assertEqual((row["status"], row["failed"], row["skipped"]), ("interrupted", 1, 1))
        self.assertTrue(all(row["active_run_id"] is None for row in await self.rows("SELECT * FROM family_api_state")))

    async def test_generic_failure_rolls_back_entity_checkpoint_and_fields(self):
        data = sample("battle_log")
        with patch("elbow_helper.data.store.sources.collect_fields", side_effect=ValueError("synthetic")):
            _, report = await self.record(data)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(await self.rows("SELECT * FROM family_api_entities"), [])
        self.assertEqual(await self.rows("SELECT * FROM family_battles"), [])
        self.assertEqual(await self.rows("SELECT * FROM battle_strings"), [])
        self.assertEqual(await self.rows("SELECT * FROM api_records"), [])
        self.assertEqual((await self.rows("SELECT checkpoint_json FROM family_api_state"))[0]["checkpoint_json"], "{}")

    async def test_fixture_identifiers_names_urls_and_cursors_are_synthetic(self):
        def inspect(value, key=""):
            if isinstance(value, dict):
                for k, v in value.items():
                    inspect(v, k)
            elif isinstance(value, list):
                for child in value:
                    inspect(child, key)
            elif isinstance(value, str):
                if key == "tag" or key.endswith("Tag"):
                    self.assertTrue(value.startswith("#PP"))
                if key == "name" or key.endswith("Name"):
                    self.assertTrue(value.startswith("Synthetic name "))
                if value.startswith("https://"):
                    self.assertTrue(value.startswith("https://example.test/"))
                if key in {"after", "before"}:
                    self.assertTrue(value.startswith("synthetic-"))
        for data in samples():
            inspect(data["body"])

    async def test_version_six_upgrade_preserves_rows_and_failed_upgrade_rolls_back(self):
        old = self.path.parent / "version6.sqlite3"
        def seed(c):
            with closing(sqlite3.connect(old)) as target:
                run_sqlite_migrations(target, MIGRATIONS[:6], target_version=6)
                target.execute("INSERT INTO accounts(player_tag,first_observed_at,tracking_status) VALUES ('#PP0',1,'observed')")
                target.commit()
        await self.store.call(seed)
        def fail(c):
            MIGRATIONS[6].apply(c)
            raise ValueError("synthetic failure")
        with patch("elbow_helper.data.store.MIGRATIONS", (*MIGRATIONS[:6], SQLiteMigration(7, "failed", fail), *MIGRATIONS[7:])):
            with self.assertRaises(SQLiteMigrationError):
                await FamilyStore(old).initialize()
        def inspect(c):
            with closing(sqlite3.connect(old)) as target:
                return target.execute("PRAGMA user_version").fetchone()[0], target.execute("SELECT name FROM sqlite_master WHERE name='family_api_state'").fetchall()
        self.assertEqual(await self.store.call(inspect), (6, []))
        upgraded = FamilyStore(old)
        await upgraded.initialize()
        self.assertEqual(await upgraded.call(lambda c: c.execute("PRAGMA user_version").fetchone()[0]), SCHEMA_VERSION)
        self.assertEqual(await upgraded.call(lambda c: c.execute("SELECT player_tag FROM accounts").fetchone()[0]), "#PP0")
        await FamilyStore(old).initialize()


class SourceCollectorTests(unittest.IsolatedAsyncioTestCase):
    rows = SourceTests.rows
    seed_account = SourceTests.seed_account

    async def asyncSetUp(self):
        await SourceTests.asyncSetUp(self)
        await self.seed_account()
        self.client = SimpleNamespace(get=AsyncMock())
        self.owner = FamilyCollectors(SimpleNamespace(boot_complete=asyncio.Event()), self.store, self.client, CLANS)

    async def test_one_endpoint_failure_only_makes_its_own_run_partial(self):
        data = sample("catalogue")
        async def fetch(path, **options):
            if path.startswith("/labels/clans"):
                raise TimeoutError("private detail")
            if path.startswith("/goldpass"):
                return response(sample("goldpass")["body"])
            return response(data["body"])
        self.client.get.side_effect = fetch
        with self.assertLogs("elbow_helper.data.collectors.service", level="WARNING"):
            result = await collect_sources(self.owner, "reference_data")
        self.assertEqual((result["status"], result["failed"], result["succeeded"]), ("partial", 1, 8))
        self.assertNotIn("private detail", str(await self.rows("SELECT * FROM collection_items")))
        self.client.get.side_effect = None
        self.client.get.return_value = response(sample("league_history")["body"])
        result = await collect_sources(self.owner, "league_history")
        self.assertEqual(result["failed"], 0)
        self.assertEqual([r["status"] for r in await self.rows("SELECT status FROM collection_runs WHERE collector IN ('reference_data','league_history') ORDER BY id")], ["partial", "ok"])

    async def test_daily_reference_budget_survives_restart_including_failure(self):
        self.client.get.return_value = response(None, 503)
        with self.assertLogs("elbow_helper.data.collectors.service", level="WARNING"):
            await collect_sources(self.owner, "reference_data")
        count = self.client.get.await_count
        self.owner.store = FamilyStore(self.path)
        await collect_sources(self.owner, "reference_data")
        self.assertEqual(self.client.get.await_count, count)
        self.assertEqual(count, 9)

    async def test_paginated_raids_keep_all_pages_and_resume_at_saved_cursor(self):
        data = sample("capital_raids")["body"]
        first, second = deepcopy(data), deepcopy(data)
        first["items"] = first["items"][:1]
        second["items"] = second["items"][1:2]
        first["paging"] = {"cursors": {"after": "cursor /+?"}}
        second["paging"] = {"cursors": {}}
        self.owner.clans = CLANS[:1]
        self.client.get.side_effect = [response(first), response(second)]
        result = await collect_sources(self.owner, "capital_raids")
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["succeeded"], 2)
        self.assertIn("after=cursor+%2F%2B%3F", self.client.get.await_args_list[1].args[0])
        self.assertEqual(len(await self.rows("SELECT * FROM family_api_entities")), 2)
        self.assertEqual((await self.rows("SELECT expected FROM collection_runs WHERE collector='capital_raids'"))[0]["expected"], 2)
        self.assertTrue(json.loads((await self.rows("SELECT checkpoint_json FROM family_api_state"))[0]["checkpoint_json"])["history_complete"])
        self.assertEqual(await self.rows("PRAGMA foreign_key_check"), [])

    async def test_failed_raid_page_retains_checkpoint_and_retries_that_page(self):
        first = sample("capital_raids")["body"]
        first["paging"] = {"cursors": {"after": "synthetic-next"}}
        self.owner.clans = CLANS[:1]
        self.client.get.side_effect = [response(first), response(None, 503)]
        with self.assertLogs("elbow_helper.data.collectors.service", level="WARNING"):
            report = await collect_sources(self.owner, "capital_raids")
        self.assertEqual(report["status"], "partial")
        checkpoint = json.loads((await self.rows("SELECT checkpoint_json FROM family_api_state"))[0]["checkpoint_json"])
        self.assertEqual(checkpoint["after"], "synthetic-next")
        next_page = deepcopy(first)
        next_page["paging"] = {"cursors": {}}
        self.client.get.side_effect = [response(next_page)]
        with patch("elbow_helper.data.collectors.sources.now_us", return_value=BASE + 100 * DAY):
            await collect_sources(self.owner, "capital_raids")
        self.assertIn("after=synthetic-next", self.client.get.await_args_list[-1].args[0])

    async def test_raid_backfill_is_bounded_and_repeated_cursors_fail_only_that_page(self):
        first = sample("capital_raids")["body"]
        self.owner.clans = CLANS[:1]
        pages = []
        for index in range(10):
            body = deepcopy(first)
            body["paging"] = {"cursors": {"after": f"synthetic-{index}"}}
            pages.append(response(body))
        self.client.get.side_effect = pages
        result = await collect_sources(self.owner, "capital_raids")
        self.assertEqual(result["failed"], 0)
        self.assertEqual(self.client.get.await_count, 10)
        state = (await self.rows("SELECT * FROM family_api_state"))[0]
        self.assertEqual(json.loads(state["checkpoint_json"])["after"], "synthetic-9")
        self.client.get.side_effect = None
        self.client.get.return_value = pages[-1]
        with patch("elbow_helper.data.collectors.sources.now_us", return_value=state["next_due_at"]):
            with self.assertLogs("elbow_helper.data.collectors.service", level="WARNING"):
                result = await collect_sources(self.owner, "capital_raids")
        self.assertEqual(result["failed"], 1)

    async def test_source_cancellation_finishes_interrupted_run(self):
        entered = asyncio.Event()
        async def fetch(path, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.client.get.side_effect = fetch
        task = asyncio.create_task(collect_sources(self.owner, "battle_logs"))
        await entered.wait()
        with self.assertLogs("elbow_helper.data.collectors.service", level="WARNING"):
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        run = (await self.rows("SELECT * FROM collection_runs WHERE collector='battle_logs'"))[0]
        self.assertEqual((run["status"], run["failed"]), ("interrupted", 1))

    async def test_source_database_work_stays_off_loop_and_shutdown_waits_for_fetch(self):
        import elbow_helper.data.store as module
        original, threads = module.sqlite_connection, []
        def connect(*args, **kwargs):
            threads.append(threading.get_ident())
            return original(*args, **kwargs)
        entered, release = asyncio.Event(), asyncio.Event()
        async def fetch(path, **kwargs):
            entered.set()
            await release.wait()
            return response({"items": []})
        self.client.get.side_effect = fetch
        with patch.object(module, "sqlite_connection", side_effect=connect):
            task = asyncio.create_task(collect_sources(self.owner, "battle_logs"))
            await entered.wait()
            close = asyncio.create_task(self.owner.cog_unload())
            await asyncio.sleep(0)
            self.assertFalse(close.done())
            release.set()
            await task
            await close
        self.assertTrue(threads)
        self.assertNotIn(threading.get_ident(), threads)
        self.client.get.reset_mock()
        await collect_sources(self.owner, "battle_logs")
        self.client.get.assert_not_awaited()

    async def test_old_ranked_500_uses_one_attempt_and_is_not_polled_again(self):
        run = (await self.rows("SELECT id FROM collection_runs"))[0]["id"]
        def seed(c):
            with sqlite_transaction(c):
                c.execute("INSERT INTO player_ranked_references VALUES (1,?,NULL,NULL,'#PP9',123,?)", (BASE, run))
        await self.store.call(seed)
        self.client.get.return_value = response({"reason": "unknownException"}, 500)
        with self.assertLogs("elbow_helper.data.collectors.service", level="WARNING"):
            await collect_sources(self.owner, "ranked_groups")
        self.client.get.assert_awaited_once_with("/leaguegroup/%23PP9/123?playerTag=%23P0", attempts=1, fresh=True)
        self.assertEqual(await self.store.call(due_sources, "ranked_groups", CLANS, BASE + 10 * DAY), [])
