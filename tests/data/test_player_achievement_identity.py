"""Real player shapes and distinct achievements sharing a displayed name."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from elbow_helper.data.store.player_projection import project_profile

from . import test_players as player_tests


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
