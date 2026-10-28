"""Real player shapes and distinct achievements sharing a displayed name."""

import asyncio
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.data.collectors.service import FamilyCollectors
from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store.clans import collect_clans
from elbow_helper.data.store.player_projection import project_profile
from elbow_helper.data.store.schema import MIGRATIONS, SCHEMA_VERSION
from elbow_helper.infrastructure.persistence.migrations import SQLiteMigrationError, run_sqlite_migrations

from . import test_players as player_tests
from .test_family_store import CLANS, clan_response, player, response


def real_profiles():
    fixtures = Path(__file__).parents[1] / "fixtures" / "family_api"
    return [json.loads((fixtures / f"player_{index}.json").read_text(encoding="utf-8"))["body"]
            for index in range(1, 5)]


def shared_achievements():
    return [{"name": "Shared achievement", "village": "home", "stars": 1, "target": 10,
             "value": value, "info": info, "completionInfo": "Synthetic completion"}
            for value, info in ((3, "First instructions"), (5, "Second instructions"))]


class AchievementProjectionTests(unittest.TestCase):
    def test_each_real_profile_projects_every_achievement_as_complete(self):
        for payload in real_profiles():
            with self.subTest(tag=payload["tag"]):
                projected = project_profile(payload, payload["tag"])
                self.assertTrue(projected.complete)
                self.assertEqual(projected.items["achievements"], payload["achievements"])

    def test_shared_names_with_distinct_instructions_or_villages_are_valid(self):
        payload = player_tests.profile()
        payload["achievements"] = shared_achievements()
        payload["achievements"].append({**payload["achievements"][0], "village": "builderBase"})
        projected = project_profile(payload, "#P0")
        self.assertTrue(projected.complete)
        self.assertEqual(len(projected.items["achievements"]), 3)

    def test_conflicting_identities_still_make_the_array_incomplete(self):
        for source, infos in (("achievements", ("Same", "Same")),
                              ("achievements", (None, {})),
                              ("troops", ("First", "Second"))):
            with self.subTest(source=source, infos=infos):
                payload = player_tests.profile()
                rows = shared_achievements() if source == "achievements" else [deepcopy(payload[source][0])] * 2
                payload[source] = [{**row, "info": info} for row, info in zip(rows, infos)]
                projected = project_profile(payload, "#P0")
                self.assertFalse(projected.complete)
                self.assertFalse(projected.complete_arrays[source])
                self.assertEqual(projected.items[source], [])


class AchievementStoreTests(player_tests.PlayerTestCase):
    async def test_real_profile_collection_records_four_successes_and_no_failures(self):
        profiles = await asyncio.to_thread(real_profiles)
        specs = [(kind, clan.tag) for clan in CLANS for kind in ("clan_profile", "clan_roster")]
        run = await self.store.begin_run("clans", "family_clans", specs, 10)
        await self.store.call(collect_clans, run, CLANS,
                              [clan_response(0, [player(payload["tag"]) for payload in profiles]),
                               clan_response(1, [])], 10)
        await self.store.finish(run)
        by_url = {f"/players/%23{payload['tag'][1:]}": payload for payload in profiles}
        client = SimpleNamespace(get=AsyncMock(side_effect=lambda url: response(by_url[url])))
        collectors = FamilyCollectors(SimpleNamespace(boot_complete=asyncio.Event()), self.store, client, CLANS)
        with patch("elbow_helper.data.collectors.service.now_us", return_value=100):
            result = await collectors.collect_players()
        self.assertEqual(result, {"status": "ok", "failed": 0, "succeeded": 4, "empty": 0})
        self.assertEqual(client.get.await_count, 4)
        self.assertEqual(set(by_url), {call.args[0] for call in client.get.await_args_list})
        self.assertEqual(await self.rows("SELECT * FROM collection_items WHERE source_kind='player_profile' AND status='failed'"), [])
        for payload in profiles:
            items = await self.rows(
                "SELECT p.* FROM player_items p JOIN item_catalog c ON c.id=p.item_id "
                "JOIN accounts a ON a.id=p.account_id WHERE a.player_tag=? AND c.source_array='achievements'",
                (payload["tag"],))
            self.assertEqual(len(items), len(payload["achievements"]))
        coverage = await self.rows("SELECT completeness_json FROM player_coverage")
        self.assertEqual(len(coverage), 4)
        self.assertTrue(all(json.loads(row["completeness_json"])["complete"] for row in coverage))

    async def test_reorder_progress_removal_and_conflicts_keep_distinct_histories(self):
        payload = player_tests.profile()
        payload["achievements"] = shared_achievements()
        self.assertEqual((await self.observe(100, payload))["failed"], 0)
        catalog = await self.rows("SELECT id,identity_key FROM item_catalog WHERE source_array='achievements' ORDER BY id")
        self.assertEqual(len(catalog), 2)
        ids = {row["identity_key"]: str(row["id"]) for row in catalog}
        progress = (await self.rows("SELECT achievement_values_json FROM player_collection_state"))[0]
        self.assertEqual(json.loads(progress["achievement_values_json"]),
                         {ids["First instructions"]: 3, ids["Second instructions"]: 5})
        payload["achievements"].reverse()
        payload["achievements"][0]["value"] += 2
        self.assertEqual((await self.observe(player_tests.DAY_US + 100, payload))["failed"], 0)
        self.assertEqual(await self.rows("SELECT id,identity_key FROM item_catalog WHERE source_array='achievements' ORDER BY id"), catalog)
        self.assertEqual(await self.rows("SELECT * FROM player_items WHERE observed_at=?", (player_tests.DAY_US + 100,)), [])
        progress = (await self.rows("SELECT achievement_values_json FROM player_collection_state"))[0]
        self.assertEqual(json.loads(progress["achievement_values_json"]),
                         {ids["First instructions"]: 3, ids["Second instructions"]: 7})
        # Conflicting copies cannot erase either achievement or its progress.
        conflicting = deepcopy(payload)
        conflicting["achievements"] = [deepcopy(payload["achievements"][0])] * 2
        self.assertEqual((await self.observe(2 * player_tests.DAY_US + 100, conflicting))["failed"], 1)
        self.assertEqual(await self.rows("SELECT * FROM player_items WHERE present=0"), [])
        self.assertEqual((await self.rows("SELECT achievement_values_json FROM player_collection_state"))[0], progress)
        payload["achievements"].pop()
        self.assertEqual((await self.observe(3 * player_tests.DAY_US + 100, payload))["failed"], 0)
        removed = await self.rows("SELECT item_id FROM player_items WHERE present=0")
        self.assertEqual(removed, [{"item_id": int(ids["First instructions"])}])
        self.assertEqual(await self.rows("PRAGMA foreign_key_check"), [])

    async def test_version_three_migration_preserves_ids_facts_and_equipment_references(self):
        old_path = self.path.parent / "version_three.sqlite3"

        def seed():
            with closing(sqlite3.connect(old_path)) as connection:
                run_sqlite_migrations(connection, MIGRATIONS[:3], target_version=3)
                connection.execute("INSERT INTO accounts VALUES (1,'#P0','Player',1,1,'observed')")
                connection.execute("INSERT INTO collection_runs(id,collector,scope_kind,scheduled_at,started_at,finished_at,status,expected) "
                                   "VALUES (1,'players','accounts',1,1,1,'ok',1)")
                for item_id, source, name in ((1, "achievements", "Shared achievement"),
                                              (2, "heroes", "Hero"), (3, "heroEquipment", "Equipment")):
                    connection.execute("INSERT INTO item_catalog VALUES (?,?,'home',?,NULL,NULL,'{}')", (item_id, source, name))
                    connection.execute("INSERT INTO player_items VALUES (1,?,1,1,10,?,1,NULL,1)",
                                       (item_id, json.dumps({"info": "First instructions", "stars": 1, "target": 10})))
                connection.execute("INSERT INTO hero_equipment VALUES (1,2,0,1,3,NULL,1)")
                connection.execute("INSERT INTO player_collection_state(account_id,achievement_values_json) VALUES (1,?)", ('{"1":3}',))
                connection.commit()

        await asyncio.to_thread(seed)
        self.store = FamilyStore(old_path)
        await self.store.initialize()
        self.assertEqual(await self.rows("PRAGMA user_version"), [{"user_version": SCHEMA_VERSION}])
        self.assertEqual(await self.rows("SELECT id,identity_key FROM item_catalog ORDER BY id"),
                         [{"id": 1, "identity_key": "First instructions"}, {"id": 2, "identity_key": ""},
                          {"id": 3, "identity_key": ""}])
        self.assertEqual(await self.rows("SELECT item_id,source_run_id FROM player_items ORDER BY item_id"),
                         [{"item_id": index, "source_run_id": 1} for index in (1, 2, 3)])
        self.assertEqual(await self.rows("SELECT hero_item_id,equipment_item_id FROM hero_equipment"),
                         [{"hero_item_id": 2, "equipment_item_id": 3}])
        self.assertEqual(await self.rows("SELECT achievement_values_json FROM player_collection_state"),
                         [{"achievement_values_json": '{"1":3}'}])
        self.assertEqual(await self.rows("PRAGMA foreign_key_check"), [])
        await FamilyStore(old_path).initialize()

    async def test_catalog_migration_failure_rolls_back_old_schema_and_facts(self):
        old_path = self.path.parent / "invalid_version_three.sqlite3"

        def seed():
            with closing(sqlite3.connect(old_path)) as connection:
                run_sqlite_migrations(connection, MIGRATIONS[:3], target_version=3)
                connection.execute("INSERT INTO accounts VALUES (1,'#P0','Player',1,1,'observed')")
                connection.execute("INSERT INTO player_items VALUES (1,999,1,NULL,NULL,'{}',1,NULL,999)")
                connection.commit()

        await asyncio.to_thread(seed)
        with self.assertRaises(SQLiteMigrationError):
            await FamilyStore(old_path).initialize()

        def inspect():
            with closing(sqlite3.connect(old_path)) as connection:
                return (connection.execute("PRAGMA user_version").fetchone()[0],
                        [row[1] for row in connection.execute("PRAGMA table_info(item_catalog)")],
                        connection.execute("SELECT item_id FROM player_items").fetchone()[0],
                        connection.execute("SELECT name FROM sqlite_master WHERE name='item_catalog_expanded'").fetchall())

        version, columns, item_id, replacements = await asyncio.to_thread(inspect)
        self.assertEqual(version, 3)
        self.assertNotIn("identity_key", columns)
        self.assertEqual(item_id, 999)
        self.assertEqual(replacements, [])
