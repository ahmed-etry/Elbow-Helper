"""Offline profiles, conservative removals, request scheduling and growth checks."""

import asyncio
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import threading
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.data.collectors.service import FamilyCollectors
from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store.clans import collect_clans
from elbow_helper.data.store.people import account, mirror_links
from elbow_helper.data.store.player_projection import HOUR_US, project_profile
from elbow_helper.data.store.players import collect_player, due_accounts, players_due
from elbow_helper.data.store.schema import create_initial_schema
from elbow_helper.data.store.schema import create_player_schema
from elbow_helper.data.store.schema import SCHEMA_VERSION
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction

from .test_family_store import CLANS, clan_response, link, player, response
from .player_size import annual_size


DAY_US = 24 * HOUR_US


def profile(tag="#P0"):
    fixture = Path(__file__).parents[1] / "fixtures" / "clan_health_player.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    payload.update(tag=tag, role="member", warPreference="in", builderHallLevel=10,
                   builderBaseTrophies=4000, bestBuilderBaseTrophies=4200,
                   labels=[{"id": 1, "name": "Synthetic label"}],
                   clan={"tag": "#PY", "name": "First clan"},
                   leagueTier={"id": 1, "name": "Synthetic tier"},
                   playerHouse={"elements": [{"type": "roof", "id": 1}]},
                   currentRankedGroup={"tag": "#QY"},
                   legendStatistics={"bestSeason": {"id": "2026-09", "trophies": 6000},
                                     "currentSeason": {"id": "2026-10", "trophies": 5000}})
    payload["heroes"][0]["equipment"] = [deepcopy(payload["heroEquipment"][0])]
    return payload


def realistic_profile():
    payload = profile()
    # 208 named home/builder units, heroes, equipment, spells and achievements.
    for source, count in (("troops", 88), ("heroes", 8), ("heroEquipment", 40),
                          ("spells", 18), ("achievements", 54)):
        rows = payload[source]
        while len(rows) < count:
            index = len(rows)
            row = {"name": f"Synthetic {source} {index}", "village": "builderBase" if index % 4 == 0 else "home"}
            row.update({"stars": 2, "value": 10000 + index, "target": 100000,
                        "info": "Synthetic progress", "completionInfo": "Synthetic completion"}
                       if source == "achievements" else {"level": 10, "maxLevel": 20})
            rows.append(row)
    return payload


class PlayerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "family.sqlite3"
        self.store = FamilyStore(self.path)
        await self.store.initialize()
        def seed(connection):
            with sqlite_transaction(connection):
                return account(connection, "#P0", 1)

        self.account_id = await self.store.call(seed)

    async def rows(self, sql, args=()):
        return await self.store.call(lambda c: [dict(row) for row in c.execute(sql, args)])

    async def observe(self, at, payload=None, *, fetched=None):
        run = await self.store.begin_run("players", "accounts", [("player_profile", "#P0")], at)
        with patch("elbow_helper.data.store.now_us", return_value=at):
            await self.store.requested(run, 0)
        await self.store.call(collect_player, run, 0, self.account_id, "#P0",
                              fetched if fetched is not None else response(profile() if payload is None else payload), at)
        return await self.store.finish(run)


class PlayerStoreTests(PlayerTestCase):
    async def test_quiet_hours_keep_one_counter_row_and_no_new_receipts(self):
        await self.observe(100)
        baseline = await self.rows("SELECT * FROM player_counters")
        receipts = await self.rows("SELECT * FROM collection_items")
        for hour in (1, 2, 23, 24, 25, 48):
            await self.observe(hour * HOUR_US + 100)
        self.assertEqual(await self.rows("SELECT * FROM player_counters"), baseline)
        self.assertEqual(await self.rows("SELECT * FROM collection_items"), receipts)
        self.assertEqual(len(await self.rows("SELECT * FROM player_collection_state")), 1)
        # The long polling gap creates a coverage boundary, not a false claim
        # that all the missing polls worked.
        self.assertEqual(len(await self.rows("SELECT * FROM player_coverage")), 3)
        self.assertEqual(len(await self.rows("SELECT * FROM collection_runs WHERE status='ok'")), 7)
        self.assertIn("source_run_id", baseline[0])
        self.assertNotIn("source_item_id", baseline[0])

    async def test_daily_progress_is_sparse_across_hours_restart_and_removals(self):
        payload = realistic_profile()
        await self.observe(100, payload)
        initial = (await self.rows("SELECT * FROM player_counters"))[0]
        self.assertEqual(len(json.loads(initial["achievement_values_json"])), 54)
        payload["achievements"][0]["value"] += 10
        payload["expLevel"] += 1
        await self.observe(HOUR_US + 100, payload)
        self.assertEqual(len(await self.rows("SELECT * FROM player_counters")), 1)
        self.assertEqual(len(await self.rows("SELECT * FROM player_observations WHERE observed_at=?", (HOUR_US + 100,))), 0)
        self.store = FamilyStore(self.path)
        await self.store.initialize()
        await self.observe(DAY_US + 100, payload)
        progress = (await self.rows("SELECT * FROM player_counters ORDER BY sampled_at"))[-1]
        patch_values = json.loads(progress["achievement_values_json"])
        self.assertEqual(list(patch_values.values()), [payload["achievements"][0]["value"]])
        fields = await self.rows("SELECT field_key FROM player_observations WHERE observed_at=?", (DAY_US + 100,))
        self.assertEqual(fields, [{"field_key": "expLevel"}])
        payload["achievements"][0]["value"] += 10
        await self.observe(DAY_US + HOUR_US + 100, payload)
        self.assertEqual(len(await self.rows("SELECT * FROM player_counters")), 2)
        removed = payload["achievements"].pop()
        await self.observe(2 * DAY_US + 100, payload)
        final = json.loads((await self.rows("SELECT achievement_values_json FROM player_counters ORDER BY sampled_at"))[-1]["achievement_values_json"])
        self.assertEqual(len(final), 2)
        self.assertEqual(sum(value is None for value in final.values()), 1)
        self.assertNotIn(removed["value"], [value for value in final.values() if value is not None])
        await self.observe(3 * DAY_US + 100, payload)
        self.assertEqual(len(await self.rows("SELECT * FROM player_counters")), 3)

    async def test_partial_daily_progress_does_not_remove_unknown_achievements(self):
        payload = realistic_profile()
        await self.observe(100, payload)
        baseline = json.loads((await self.rows("SELECT achievement_values_json FROM player_collection_state"))[0]["achievement_values_json"])
        payload["achievements"] = [{**payload["achievements"][0], "value": 9}, {"name": "Malformed"}]
        await self.observe(DAY_US + 100, payload)
        current = json.loads((await self.rows("SELECT achievement_values_json FROM player_collection_state"))[0]["achievement_values_json"])
        self.assertEqual(len(current), 54)
        self.assertEqual(sum(current[key] != baseline[key] for key in baseline), 1)

    async def test_failed_and_missing_polls_break_coverage_without_erasing_facts(self):
        await self.observe(100)
        await self.observe(HOUR_US + 100)
        await self.observe(2 * HOUR_US + 100, fetched=response(None, 503))
        await self.observe(3 * HOUR_US + 100)
        await self.observe(6 * HOUR_US + 100)
        self.assertEqual(len(await self.rows("SELECT * FROM player_coverage")), 3)
        self.assertEqual(len(await self.rows("SELECT * FROM collection_items WHERE status='failed'")), 1)
        self.assertEqual(len(await self.rows("SELECT * FROM player_counters")), 1)

    async def test_interrupted_player_checkpoint_recovers_without_duplicate_facts(self):
        await self.observe(100)
        run = await self.store.begin_run("players", "accounts", [("player_profile", "#P0")], HOUR_US + 100)
        await self.store.requested(run, 0)
        self.store = FamilyStore(self.path)
        await self.store.initialize()
        recovered = (await self.rows("SELECT * FROM collection_runs WHERE id=?", (run,)))[0]
        self.assertEqual((recovered["status"], recovered["failed"], recovered["attempted"]), ("interrupted", 1, 1))
        self.assertEqual(len(await self.rows("SELECT * FROM collection_items WHERE error_code='interrupted'")), 1)
        await self.observe(2 * HOUR_US + 100)
        self.assertEqual(len(await self.rows("SELECT * FROM player_counters")), 1)

    async def test_version_two_history_is_compacted_without_losing_provenance(self):
        old_path = self.path.parent / "version_two.sqlite3"
        with closing(sqlite3.connect(old_path)) as c, c:
            create_initial_schema(c)
            create_player_schema(c)
            c.execute("INSERT INTO accounts VALUES (1,'#P0','Player',100,?,'observed')", (2 * HOUR_US + 100,))
            c.execute("INSERT INTO item_catalog VALUES (1,'achievements','home','Progress',NULL,'achievement','{}')")
            complete = json.dumps({"complete": True, "fields": True, "arrays": dict.fromkeys(
                ("troops", "heroes", "heroEquipment", "spells", "achievements"), True), "equipment": {}, "unsupported_fields": []})
            for index in range(3):
                at = index * HOUR_US + 100
                c.execute("INSERT INTO collection_runs(id,collector,scope_kind,scheduled_at,started_at,finished_at,status,expected,attempted,succeeded) "
                          "VALUES (?,'players','accounts',?,?,?,'ok',1,1,1)", (index + 1, at, at, at))
                c.execute("INSERT INTO collection_items(id,run_id,ordinal,source_kind,scope_id,account_id,status,started_at,fetched_at,completeness_json) "
                          "VALUES (?,?,0,'player_profile','#P0',1,'ok',?,?,?)", (index + 1, index + 1, at, at, complete))
                c.execute("INSERT INTO player_counters VALUES (1,?,10,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,?,'{}',?,'{}',?)",
                          (at, json.dumps({"1": index + 1}), json.dumps({"fields": ["trophies"], "legendStatistics": False,
                                                                       "arrays": {"achievements": True}}), index + 1))
            c.execute("INSERT INTO player_observations VALUES (1,'name',100,'\"Player\"',NULL,1)")
            c.execute("INSERT INTO player_items VALUES (1,1,100,NULL,NULL,'{\"stars\":1,\"target\":10}',1,NULL,1)")
            c.execute("INSERT INTO account_aliases VALUES (1,'Player','player',100,NULL,1,NULL)")
            c.execute("PRAGMA user_version=2")
        self.store = FamilyStore(old_path)
        await self.store.initialize()
        self.assertEqual(len(await self.rows("SELECT * FROM player_counters")), 1)
        self.assertEqual(len(await self.rows("SELECT * FROM collection_items")), 1)
        self.assertEqual(len(await self.rows("SELECT * FROM player_coverage")), 1)
        observation = (await self.rows("SELECT * FROM player_observations"))[0]
        self.assertEqual(observation["source_run_id"], 1)
        payload = {"tag": "#P0", "name": "Player", "townHallLevel": 10, "trophies": 10,
                   "troops": [], "heroes": [], "heroEquipment": [], "spells": [],
                   "achievements": [{"name": "Progress", "village": "home", "stars": 1, "target": 10, "value": 3}]}
        await self.observe(3 * HOUR_US + 100, payload)
        self.assertEqual(len(await self.rows("SELECT * FROM player_counters")), 1)
        await self.observe(DAY_US + 100, payload)
        self.assertEqual(json.loads((await self.rows("SELECT * FROM player_counters ORDER BY sampled_at"))[-1]["achievement_values_json"]), {"1": 3})
        self.assertEqual(await self.rows("PRAGMA foreign_key_check"), [])

    async def test_migration_from_version_one_preserves_existing_rows_and_restarts(self):
        old_path = self.path.parent / "version_one.sqlite3"
        with closing(sqlite3.connect(old_path)) as c, c:
            create_initial_schema(c)
            c.execute("INSERT INTO accounts(player_tag,first_observed_at,tracking_status) VALUES ('#P0',1,'observed')")
            c.execute("PRAGMA user_version=1")
        upgraded = FamilyStore(old_path)
        await upgraded.initialize()
        self.assertEqual(await upgraded.call(lambda c: c.execute("PRAGMA user_version").fetchone()[0]), SCHEMA_VERSION)
        self.assertEqual(await upgraded.call(lambda c: c.execute("SELECT player_tag FROM accounts").fetchone()[0]), "#P0")
        await FamilyStore(old_path).initialize()
        self.assertEqual(await upgraded.call(lambda c: c.execute("PRAGMA foreign_key_check").fetchall()), [])

    async def test_unchanged_counter_growth_and_one_upgrade_only_write_one_item_change(self):
        payload = profile()
        await self.observe(100, payload)
        count = len(await self.rows("SELECT * FROM player_items"))
        fields = await self.rows("SELECT * FROM player_observations")
        payload["trophies"] += 10
        payload["achievements"][0]["value"] += 100
        payload["troops"][3]["superTroopIsActive"] = False
        payload["legendStatistics"]["currentSeason"]["trophies"] += 10
        await self.observe(HOUR_US + 100, payload)
        self.assertEqual(len(await self.rows("SELECT * FROM player_items")), count)
        self.assertEqual(await self.rows("SELECT * FROM player_observations"), fields)
        payload["troops"][0]["level"] += 1
        await self.observe(2 * HOUR_US + 100, payload)
        self.assertEqual(len(await self.rows("SELECT * FROM player_items")), count)
        await self.observe(DAY_US + 100, payload)
        changes = await self.rows("SELECT * FROM player_items WHERE observed_at=?", (DAY_US + 100,))
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["previous_good_at"], 100)
        self.assertEqual(await self.rows("PRAGMA foreign_key_check"), [])

    async def test_removed_and_reappearing_item_and_disappearing_scalar(self):
        payload = profile()
        await self.observe(100, payload)
        removed = payload["troops"].pop()
        del payload["warPreference"]
        await self.observe(DAY_US + 200, payload)
        self.assertEqual(len(await self.rows("SELECT * FROM player_items WHERE present=0")), 1)
        self.assertEqual((await self.rows("SELECT value_json FROM player_observations WHERE field_key='warPreference' ORDER BY observed_at"))[-1]["value_json"], "null")
        await self.observe(DAY_US + 300, payload)
        self.assertEqual(len(await self.rows("SELECT * FROM player_items WHERE present=0")), 1)
        payload["troops"].append(removed)
        await self.observe(2 * DAY_US + 400, payload)
        self.assertEqual(len(await self.rows("SELECT * FROM player_items WHERE observed_at=?", (2 * DAY_US + 400,))), 1)

    async def test_partial_missing_and_duplicate_arrays_cannot_remove_items(self):
        payload = profile()
        await self.observe(100, payload)
        initial = len(await self.rows("SELECT * FROM player_items"))
        for index, value in enumerate((None, {}, [{"name": "Broken"}],
                                       [payload["troops"][0], payload["troops"][0]])):
            with self.subTest(value=value):
                altered = deepcopy(payload)
                altered["troops"] = value
                report = await self.observe(200 + index, altered)
                self.assertEqual(report["status"], "failed")
        altered = deepcopy(payload)
        del altered["troops"]
        await self.observe(300, altered)
        self.assertEqual(len(await self.rows("SELECT * FROM player_items")), initial)
        self.assertEqual(await self.rows("SELECT * FROM player_items WHERE present=0"), [])
        altered["troops"] = []
        await self.observe(DAY_US + 400, altered)
        self.assertEqual(len(await self.rows("SELECT * FROM player_items WHERE present=0")), len(payload["troops"]))
        removed = (await self.rows("SELECT previous_good_at FROM player_items WHERE present=0"))[0]
        self.assertEqual(removed["previous_good_at"], 100)

    async def test_failed_missing_mismatched_and_malformed_profiles_change_no_facts(self):
        await self.observe(100)
        tables = ("player_observations", "player_items", "player_counters", "hero_equipment", "account_aliases", "accounts")
        before = {table: await self.rows(f"SELECT * FROM {table}") for table in tables}
        invalid = [response(None, 404), response(None, 503), response({}), response(profile("#Q0")),
                   response({**profile(), "trophies": True}), response({**profile(), "warPreference": None}),
                   response(profile(), status=200)]
        invalid[-1] = type(invalid[-1])(200, profile(), {}, 1, 1, ValueError("private details"))
        for at, fetched in enumerate(invalid, start=200):
            result = await self.observe(at, fetched=fetched)
            self.assertEqual(result["failed"], 1)
        self.assertEqual({table: await self.rows(f"SELECT * FROM {table}") for table in tables}, before)
        self.assertNotIn("private details", str(await self.rows("SELECT * FROM collection_items")))

    async def test_renamed_profile_closes_alias_and_new_name_is_observed(self):
        await self.observe(100)
        payload = profile()
        payload["name"] = "Renamed account"
        await self.observe(200, payload)
        aliases = await self.rows("SELECT name,from_at,to_at FROM account_aliases ORDER BY from_at")
        self.assertEqual([(row["name"], row["to_at"]) for row in aliases], [("Synthetic Player", 200), ("Renamed account", None)])

    async def test_counters_once_per_utc_hour_missing_presence_and_reset_boundaries(self):
        payload = profile()
        await self.observe(HOUR_US - 1, payload)
        await self.observe(HOUR_US - 1, payload)
        del payload["defenseWins"]
        payload["trophies"] -= 100
        payload["achievements"][0]["value"] = 1
        await self.observe(HOUR_US, payload)
        counters = await self.rows("SELECT * FROM player_counters ORDER BY sampled_at")
        self.assertEqual(len(counters), 2)
        self.assertIsNone(counters[-1]["defense_wins"])
        presence = json.loads(counters[-1]["field_presence_json"])["fields"]
        self.assertFalse(presence & (1 << 6))
        context = json.loads(counters[-1]["reset_context_json"])
        self.assertEqual(context["decreased_fields"], ["trophies"])
        self.assertNotIn("decreased_achievements", context)
        self.assertEqual(json.loads(counters[-1]["achievement_values_json"]), {})
        self.assertEqual(counters[-1]["donations"], payload["donations"])
        self.assertEqual(len(json.loads(counters[-1]["transient_item_fields_json"])), 1)
        await self.observe(DAY_US, payload)
        latest = (await self.rows("SELECT * FROM player_counters ORDER BY sampled_at"))[-1]
        self.assertEqual(len(json.loads(latest["reset_context_json"])["decreased_achievements"]), 1)

    async def test_equipment_slots_switch_order_remove_and_partial_refs_preserve_facts(self):
        payload = profile()
        payload["heroEquipment"].append({"name": "New equipment", "level": 2, "maxLevel": 10})
        hero = payload["heroes"][0]
        hero["equipment"].append(payload["heroEquipment"][1])
        await self.observe(100, payload)
        hero["equipment"].reverse()
        await self.observe(DAY_US + 200, payload)
        self.assertEqual(len(await self.rows("SELECT * FROM hero_equipment WHERE observed_at=?", (DAY_US + 200,))), 2)
        hero["equipment"] = [{"name": "Unknown equipment"}]
        result = await self.observe(2 * DAY_US + 300, payload)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(await self.rows("SELECT * FROM hero_equipment WHERE observed_at=?", (2 * DAY_US + 300,)), [])
        del hero["equipment"]
        await self.observe(3 * DAY_US + 400, payload)
        self.assertEqual(await self.rows("SELECT * FROM hero_equipment WHERE observed_at=?", (3 * DAY_US + 400,)), [])
        hero["equipment"] = []
        await self.observe(4 * DAY_US + 500, payload)
        removed = await self.rows("SELECT * FROM hero_equipment WHERE observed_at=?", (4 * DAY_US + 500,))
        self.assertEqual(len(removed), 2)
        self.assertTrue(all(row["equipment_item_id"] is None for row in removed))
        self.assertTrue(all(row["previous_good_at"] == DAY_US + 200 for row in removed))
        payload["heroes"] = []
        await self.observe(5 * DAY_US + 600, payload)
        self.assertEqual(await self.rows("SELECT * FROM hero_equipment WHERE observed_at=?", (5 * DAY_US + 600,)), [])

    async def test_api_array_identities_classify_without_pet_name_list(self):
        payload = profile()
        novel = {"name": "Future pet", "village": "home", "level": 1, "maxLevel": 10}
        payload["pets"] = [novel]
        payload["troops"].append(novel)
        await self.observe(100, payload)
        catalog = await self.rows("SELECT * FROM item_catalog")
        by_identity = {(row["source_array"], row["source_name"]): row for row in catalog}
        self.assertEqual(by_identity[("troops", "Future pet")]["classification"], "pet")
        self.assertEqual(by_identity[("pets", "Future pet")]["classification"], "pet")
        self.assertIsNone(by_identity[("troops", "L.A.S.S.I")]["classification"])
        self.assertEqual(by_identity[("heroes", "Barbarian King")]["classification"], "hero")
        self.assertEqual(by_identity[("heroEquipment", "Barbarian Puppet")]["classification"], "equipment")

    async def test_unknown_profile_and_legend_fields_do_not_fail_collection(self):
        payload = profile()
        payload["newApiField"] = {"anything": "not projected"}
        payload["legendStatistics"]["newLegendField"] = {"future": [1, None]}
        projected = project_profile(payload, "#P0")
        self.assertTrue(projected.complete)
        result = await self.observe(100, payload)
        self.assertEqual(result, {"status": "ok", "failed": 0, "succeeded": 1, "empty": 0})
        item = (await self.rows("SELECT completeness_json FROM collection_items"))[0]
        completeness = json.loads(item["completeness_json"])
        self.assertTrue(completeness["complete"])
        self.assertEqual(completeness["unsupported_fields"], ["newApiField", "legendStatistics.newLegendField"])
        self.assertEqual(await self.rows("SELECT * FROM player_observations WHERE field_key='newApiField'"), [])
        self.assertNotIn("newLegendField", projected.legend)

    async def test_unknown_item_fields_do_not_fail_any_profile_array(self):
        payload = profile()
        payload["pets"] = [{"name": "Future pet", "village": "home", "level": 1, "maxLevel": 10}]
        for source in ("troops", "pets", "heroes", "heroEquipment", "spells", "achievements"):
            payload[source][0]["newApiField"] = {"future": [1, None, "extra"]}
        projected = project_profile(payload, "#P0")
        self.assertTrue(projected.complete)
        self.assertTrue(all(projected.complete_arrays.values()))
        result = await self.observe(100, payload)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["succeeded"], 1)

    async def test_unknown_item_fields_do_not_hide_malformed_known_fields(self):
        invalid = (("troops", "level", True), ("heroes", "maxLevel", -1),
                   ("heroEquipment", "name", ""), ("spells", "village", None),
                   ("achievements", "stars", "1"), ("achievements", "target", None))
        for at, (source, key, value) in enumerate(invalid, start=100):
            with self.subTest(source=source, key=key):
                payload = profile()
                payload[source][0].update({key: value, "newApiField": {"extra": True}})
                self.assertFalse(project_profile(payload, "#P0").complete)
                result = await self.observe(at, payload)
                self.assertEqual(result["failed"], 1)

    async def test_achievement_stars_and_optional_legend_and_league_disappearance(self):
        payload = profile()
        await self.observe(100, payload)
        payload["achievements"][0]["stars"] -= 1
        del payload["legendStatistics"]
        del payload["leagueTier"]
        result = await self.observe(DAY_US + 200, payload)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(await self.rows("SELECT * FROM player_items WHERE observed_at=?", (DAY_US + 200,))), 1)
        tombstones = await self.rows("SELECT field_key FROM player_observations WHERE observed_at=? AND value_json='null'", (DAY_US + 200,))
        self.assertEqual({row["field_key"] for row in tombstones}, {"leagueTier", "legendStatistics.bestSeason"})

    async def test_explicit_equipment_slots_and_malformed_nested_arrays(self):
        payload = profile()
        payload["heroEquipment"].append({"name": "Another item", "level": 1, "maxLevel": 10})
        hero = payload["heroes"][0]
        hero["equipment"] = [{"name": payload["heroEquipment"][0]["name"], "slot": 7},
                             {"name": "Another item", "slot": 2}]
        await self.observe(100, payload)
        hero["equipment"].reverse()
        await self.observe(200, payload)
        self.assertEqual(await self.rows("SELECT * FROM hero_equipment WHERE observed_at=200"), [])
        for at, value in enumerate((None, {}, [{"name": "Another item", "slot": True}],
                                     [{"name": "Another item", "slot": 2}] * 2), start=300):
            hero["equipment"] = value
            result = await self.observe(at, payload)
            self.assertEqual(result["failed"], 1)
        self.assertEqual(len(await self.rows("SELECT * FROM hero_equipment")), 2)

    async def test_restart_does_not_duplicate_items_counters_or_aliases(self):
        await self.observe(100)
        before = {table: await self.rows(f"SELECT * FROM {table}") for table in
                  ("player_observations", "player_items", "hero_equipment", "player_counters", "account_aliases")}
        self.store = FamilyStore(self.path)
        await self.store.initialize()
        await self.observe(200)
        self.assertEqual({table: await self.rows(f"SELECT * FROM {table}") for table in before}, before)

    async def test_out_of_order_observation_rolls_back(self):
        await self.observe(200)
        with self.assertRaisesRegex(ValueError, "out_of_order"):
            await self.observe(100)
        self.assertEqual((await self.rows("SELECT last_profile_at FROM accounts"))[0]["last_profile_at"], 200)

    async def test_48_hour_size_stays_near_changes_instead_of_hourly_item_copies(self):
        payload = realistic_profile()
        self.assertEqual(sum(len(payload[source]) for source in ("troops", "heroes", "heroEquipment", "spells", "achievements")), 208)
        initial_fields = len(project_profile(payload, "#P0").fields)
        for hour in range(48):
            active = 6 <= hour % 24 < 22 and hour % 3 == 0
            if active:
                payload["trophies"] += 5
                payload["donations"] += 20
                payload["legendStatistics"]["currentSeason"]["trophies"] += 5
                for entry in payload["achievements"][:3]:
                    entry["value"] += 25
            if hour in (6, 18, 30, 42):
                payload["troops"][hour]["level"] += 1
            await self.observe(hour * HOUR_US + 100, payload)
        self.assertEqual(len(await self.rows("SELECT * FROM player_items")), 210)
        self.assertEqual(len(await self.rows("SELECT * FROM item_catalog")), 208)
        self.assertEqual(len(await self.rows("SELECT * FROM player_observations")), initial_fields)
        self.assertLess(len(await self.rows("SELECT * FROM player_counters")), 16)
        self.assertEqual(len(await self.rows("SELECT * FROM collection_items")), 1)
        self.assertEqual(len(await self.rows("SELECT * FROM player_coverage")), 1)
        # Flush the remaining upgrades at the next daily sample.
        await self.observe(48 * HOUR_US + 100, payload)
        self.assertEqual(len(await self.rows("SELECT * FROM player_items")), 212)
        counts, annual_bytes = await self.store.call(annual_size)
        self.assertEqual(counts["player_items"], 261702)
        self.assertEqual(counts["daily_progress"], 279 * 365)
        self.assertLess(counts["player_counters"], 680000)
        self.assertEqual(counts["collection_items"], 279)
        self.assertEqual(counts["player_coverage"], 279)
        # Reserve another 30 MB for aliases, equipment, resets, metadata and
        # growth in projection sizes. WAL/backups are separate working space.
        self.assertLess(annual_bytes + 30_000_000, 500_000_000)
        print(f"\nPlayer year (279 accounts): {counts}; SQLite with indexes={annual_bytes:,} bytes; "
              f"with 30 MB reserve={annual_bytes + 30_000_000:,} bytes. Failed fetches add item rows.")


class PlayerCollectorTests(PlayerTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.client = SimpleNamespace(get=AsyncMock())
        self.bot = SimpleNamespace(boot_complete=asyncio.Event())
        self.collectors = FamilyCollectors(self.bot, self.store, self.client, CLANS)

    async def roster(self, at, rows):
        specs = [(kind, clan.tag) for clan in CLANS for kind in ("clan_profile", "clan_roster")]
        run = await self.store.begin_run("clans", "family_clans", specs, at)
        await self.store.call(collect_clans, run, CLANS, [clan_response(0, rows), clan_response(1, [])], at)
        await self.store.finish(run)

    async def links(self, at, payload):
        run = await self.store.begin_run("links", "links", [("account_links", "current")], at)
        await self.store.call(mirror_links, run, payload, at)
        await self.store.finish(run)

    async def collect(self, at):
        with patch("elbow_helper.data.collectors.service.now_us", return_value=at), patch(
            "elbow_helper.data.store.now_us", return_value=at,
        ):
            return await self.collectors.collect_players()

    async def test_population_union_requests_no_clans_and_new_join_is_immediately_due(self):
        await self.roster(10, [player()])
        outside = link()["#P0"]
        outside.update(player_tag="#Q0")
        await self.links(20, {"#P0": link()["#P0"], "#Q0": outside})
        self.client.get.side_effect = [response(profile()), response(profile("#Q0"))]
        result = await self.collect(100)
        self.assertEqual(result["succeeded"], 2)
        self.assertEqual([call.args[0] for call in self.client.get.await_args_list], ["/players/%23P0", "/players/%23Q0"])
        self.assertFalse(await self.store.call(players_due, 200))
        await self.roster(300, [player(), player("#Y0")])
        self.assertTrue(await self.store.call(players_due, 301))
        self.client.get.side_effect = [response(profile("#Y0"))]
        result = await self.collect(301)
        self.assertEqual(result["succeeded"], 1)
        self.client.get.assert_awaited_with("/players/%23Y0")
        self.store = FamilyStore(self.path)
        await self.store.initialize()
        self.assertEqual(await self.store.call(due_accounts, 400), [])
        await self.roster(500, [])
        self.assertEqual([row["player_tag"] for row in await self.store.call(due_accounts, HOUR_US + 501)], ["#P0", "#Q0"])

    async def test_fetch_exception_and_404_record_each_account_and_continue(self):
        await self.roster(10, [player(), player("#Q0"), player("#Y0")])
        self.client.get.side_effect = [TimeoutError("private"), response(None, 404), response(profile("#Y0"))]
        with self.assertLogs("elbow_helper.data.collectors.service", level="WARNING") as logs:
            result = await self.collect(100)
        self.assertEqual(result, {"status": "partial", "failed": 2, "succeeded": 1, "empty": 0})
        self.assertEqual(len(logs.output), 1)
        items = await self.rows("SELECT * FROM collection_items WHERE source_kind='player_profile' ORDER BY ordinal")
        self.assertEqual([row["error_code"] for row in items], ["fetch_failed", "profile_missing", None])
        self.assertTrue(all(row["account_id"] is not None for row in items))
        self.assertEqual([row["player_tag"] for row in await self.store.call(due_accounts, HOUR_US + 100)], ["#P0", "#Y0"])
        self.assertEqual(len(await self.store.call(due_accounts, 24 * HOUR_US + 100)), 3)

    async def test_cancelled_profile_pass_marks_unattempted_accounts_skipped(self):
        await self.roster(10, [player(), player("#Q0")])
        entered = asyncio.Event()

        async def wait(path):
            entered.set()
            await asyncio.Event().wait()

        self.client.get.side_effect = wait
        task = asyncio.create_task(self.collect(100))
        await entered.wait()
        with self.assertLogs("elbow_helper.data.collectors.service", level="WARNING"):
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        run = (await self.rows("SELECT * FROM collection_runs WHERE collector='players'"))[0]
        self.assertEqual((run["status"], run["failed"], run["skipped"]), ("interrupted", 1, 1))

    async def test_player_schedule_checks_new_accounts_and_empty_population_hourly(self):
        self.assertTrue(await self.store.call(players_due, 100))
        result = await self.collect(100)
        self.assertEqual(result["status"], "empty")
        self.client.get.assert_not_awaited()
        self.assertFalse(await self.store.call(players_due, 101))
        self.assertTrue(await self.store.call(players_due, HOUR_US + 100))
        await self.collectors.cog_unload()
        await self.collectors.collect_players()
        self.assertEqual(len(await self.rows("SELECT * FROM collection_runs")), 1)

    async def test_account_list_failure_warns_and_profile_sqlite_work_stays_off_loop(self):
        with patch.object(self.store, "call", new=AsyncMock(side_effect=OSError("unavailable"))):
            with self.assertLogs("elbow_helper.data.collectors.service", level="WARNING") as logs:
                self.assertIsNone(await self.collect(100))
        self.assertEqual(len(logs.output), 1)
        self.client.get.assert_not_awaited()
        await self.roster(10, [player()])
        self.client.get.return_value = response(profile())
        import elbow_helper.data.store as store_module
        original = store_module.sqlite_connection
        threads = []

        def connect(*args, **kwargs):
            threads.append(threading.get_ident())
            return original(*args, **kwargs)

        with patch.object(store_module, "sqlite_connection", side_effect=connect):
            await self.collect(100)
        self.assertTrue(threads)
        self.assertNotIn(threading.get_ident(), threads)

    async def test_scheduler_checks_player_account_state_on_each_tick(self):
        self.bot.boot_complete.set()
        collect = AsyncMock()

        async def stop(seconds):
            self.collectors._closing = True

        with patch("elbow_helper.data.collectors.service.asyncio.sleep", new=stop):
            await self.collectors._schedule("players", 3600, collect)
        collect.assert_awaited_once()
