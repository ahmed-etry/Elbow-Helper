"""Sanitized captured shapes, unknown-field growth and lossless history guards."""

from copy import deepcopy
import json
from pathlib import Path
from tests.temp_directory import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.data.store_fixtures import empty_store
from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store.api_fields import collect_fields, leaves, read_fields, unpack
from elbow_helper.data.store.clans import collect_clans
from elbow_helper.data.store.people import account
from elbow_helper.data.store.player_projection import HOUR_US
from elbow_helper.data.store.players import collect_player
from elbow_helper.data.store.wars import record_response
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction

from .test_family_store import response


FIXTURES = Path(__file__).parents[1] / "fixtures" / "family_api"
DAY = 24 * HOUR_US


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def leaf_count(value):
    if isinstance(value, dict) and value:
        return sum(leaf_count(child) for child in value.values())
    if isinstance(value, list) and value:
        return sum(leaf_count(child) for child in value)
    return 1


class ApiFieldTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "family.sqlite3"
        self.store = await empty_store(self.path)

    async def rows(self, sql, args=()):
        return await self.store.call(lambda c: [dict(r) for r in c.execute(sql, args)])

    async def record(self, sample, at=100, *, status=200):
        kind, scope, body = (sample[key] for key in ("kind", "scope", "body"))
        if kind == "player_profile":
            def seed(c):
                with sqlite_transaction(c):
                    return account(c, scope, 1)
            account_id = await self.store.call(seed)
            run = await self.store.begin_run("players", "accounts", [(kind, scope)], at)
            await self.store.requested(run, 0)
            await self.store.call(collect_player, run, 0, account_id, scope, response(body, status), at)
        elif kind == "clan_profile":
            clan = SimpleNamespace(tag=scope, name=body["name"], code="SYN" + scope[1:])
            run = await self.store.begin_run("clans", "family_clans", [(kind, scope), ("clan_roster", scope)], at)
            await self.store.call(collect_clans, run, [clan], [response(body, status)], at)
        else:
            def seed(c):
                with sqlite_transaction(c):
                    c.execute("INSERT OR IGNORE INTO war_collection_state(source_kind,scope_id,next_due_at) VALUES (?,?,?)",
                              (kind, scope, at))
            await self.store.call(seed)
            run = await self.store.begin_run("wars", "family_wars", [(kind, scope)], at)
            await self.store.requested(run, 0)
            family = {scope}
            if kind == "cwl_war":
                family.add(body["clan"]["tag"])
            await self.store.call(record_response, run, 0, kind, scope, response(body, status), family, at)
        return run, await self.store.finish(run)

    async def test_each_projection_preserves_leaves_and_unchanged_poll(self):
        paths = ("clan_1.json", "player_4.json", "current_war_inWar.json", "cwl_group_1.json",
                 "cwl_war_1.json", "war_log_1.json")
        for index, name in enumerate(paths):
            path = FIXTURES / name
            sample = fixture(path.name)
            with self.subTest(fixture=path.name):
                run, result = await self.record(sample, (index + 1) * DAY)
                self.assertEqual(result["failed"], 0, path.name)
                expected = {p: v for p, v, _ in leaves(sample["body"])}
                self.assertEqual(len(expected), leaf_count(sample["body"]))
                stored = await self.store.call(read_fields, sample["kind"], sample["scope"])
                for path, value in expected.items():
                    self.assertIn(path, stored)
                    self.assertEqual(stored[path], value)
                history = await self.rows("SELECT * FROM api_field_changes")
                self.assertTrue(any(row["source_run_id"] == run for row in history))
                # A page cursor is expected to advance; reset just its bounded
                # checkpoint so this synthetic repeat is the same request.
                if sample["kind"] == "war_log":
                    def reset(c):
                        with sqlite_transaction(c):
                            c.execute("UPDATE war_collection_state SET checkpoint_json='{}' WHERE source_kind='war_log' AND scope_id=?",
                                      (sample["scope"],))
                    await self.store.call(reset)
                await self.record(sample, (index + 1) * DAY + HOUR_US)
                self.assertEqual(history, await self.rows("SELECT * FROM api_field_changes"))
        self.assertEqual(await self.rows("PRAGMA foreign_key_check"), [])

    async def test_future_fields_at_all_profile_depths_and_typed_ranked_references(self):
        sample = fixture("player_4.json")
        body = sample["body"]
        body["future"] = {"nothing": None, "array": [True, 1, "one", {}, []]}
        body["troops"][0]["future"] = {"level": 7}
        body["achievements"][0]["future"] = [1, 2]
        body["legendStatistics"] = {"future": {"points": 5}}
        body.update(currentLeagueGroupTag="#P0", currentLeagueSeasonId=123,
                    previousLeagueGroupTag="#P2", previousLeagueSeasonId=122)
        _, result = await self.record(sample)
        self.assertEqual(result["failed"], 0)
        typed = (await self.rows("SELECT * FROM player_ranked_references"))[0]
        self.assertEqual([typed[key] for key in ("current_group_tag", "current_season_id", "previous_group_tag", "previous_season_id")],
                         ["#P0", 123, "#P2", 122])
        stored = await self.store.call(read_fields, sample["kind"], sample["scope"])
        self.assertEqual(stored["/future/array/0"], True)
        self.assertIsNone(stored["/future/nothing"])
        self.assertEqual(stored["/legendStatistics/future/points"], 5)

    async def test_ranked_references_change_daily_and_keep_removed_references(self):
        sample = fixture("player_4.json")
        sample["body"].update(currentLeagueGroupTag="#P0", currentLeagueSeasonId=123,
                              previousLeagueGroupTag="#P2", previousLeagueSeasonId=122)
        await self.record(sample)
        original = await self.rows("SELECT * FROM player_ranked_references")
        await self.record(sample, HOUR_US)
        self.assertEqual(original, await self.rows("SELECT * FROM player_ranked_references"))
        sample["body"]["currentLeagueSeasonId"] = 124
        await self.record(sample, 2 * HOUR_US)
        self.assertEqual(original, await self.rows("SELECT * FROM player_ranked_references"))
        await self.record(sample, DAY)
        for key in ("currentLeagueGroupTag", "currentLeagueSeasonId", "previousLeagueGroupTag", "previousLeagueSeasonId"):
            sample["body"].pop(key)
        await self.record(sample, 2 * DAY)
        rows = await self.rows("SELECT * FROM player_ranked_references ORDER BY observed_at")
        self.assertEqual([r["current_season_id"] for r in rows], [123, 124, None])
        self.assertIsNone(rows[-1]["previous_group_tag"])

    async def test_donations_are_hourly_change_only_and_keep_season_reset_after_restart(self):
        sample = fixture("clan_1.json")
        member = sample["body"]["memberList"][0]
        member.update(donations=100, donationsReceived=50)
        await self.record(sample)
        baseline = await self.rows("SELECT * FROM donation_snapshots")
        await self.record(sample, HOUR_US)
        self.assertEqual(baseline, await self.rows("SELECT * FROM donation_snapshots"))
        member["donations"] = 101
        run, _ = await self.record(sample, 2 * HOUR_US)
        changed = await self.rows("SELECT * FROM donation_snapshots WHERE sampled_at=?", (2 * HOUR_US,))
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["donations"], 101)
        receipt = (await self.rows("SELECT run_id FROM collection_items WHERE id=?", (changed[0]["source_item_id"],)))[0]
        self.assertEqual(receipt["run_id"], run)
        member["donations"] = 102
        await self.record(sample, 2 * HOUR_US + HOUR_US // 4)
        self.assertEqual(len(await self.rows("SELECT * FROM donation_snapshots")), len(baseline) + 1)
        await self.record(sample, 3 * HOUR_US)
        self.store = FamilyStore(self.path)
        await self.store.initialize()
        member.update(donations=0, donationsReceived=0)
        await self.record(sample, 4 * HOUR_US)
        samples = await self.rows("SELECT donations,received FROM donation_snapshots WHERE account_id=? ORDER BY sampled_at",
                                  (changed[0]["account_id"],))
        self.assertEqual([(r["donations"], r["received"]) for r in samples], [(100, 50), (101, 50), (102, 50), (0, 0)])
        await self.record(sample, 5 * HOUR_US)
        self.assertEqual(len(await self.rows("SELECT * FROM donation_snapshots")), len(baseline) + 3)

    async def test_partial_donation_evidence_and_failure_preserve_good_rows(self):
        sample = fixture("clan_1.json")
        await self.record(sample)
        baseline = await self.rows("SELECT * FROM donation_snapshots")
        await self.record(sample, HOUR_US, status=503)
        self.assertEqual(baseline, await self.rows("SELECT * FROM donation_snapshots"))
        broken = deepcopy(sample)
        broken["body"]["memberList"][0]["donations"] = -1
        _, result = await self.record(broken, 2 * HOUR_US)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(baseline, await self.rows("SELECT * FROM donation_snapshots"))
        sample["body"]["memberList"][0].pop("donations")
        await self.record(sample, 3 * HOUR_US)
        changed = await self.rows("SELECT * FROM donation_snapshots WHERE sampled_at=?", (3 * HOUR_US,))
        self.assertEqual(len(changed), 1)
        self.assertIsNone(changed[0]["donations"])

    async def test_hourly_fast_daily_slow_and_restart_keep_only_changed_leaves(self):
        sample = fixture("player_4.json")
        await self.record(sample)
        baseline = len(await self.rows("SELECT * FROM api_field_changes"))
        sample["body"]["trophies"] += 1
        sample["body"]["achievements"][0]["value"] += 1
        sample["body"]["future"] = {"counter": 7}
        await self.record(sample, 200)
        self.assertEqual(len(await self.rows("SELECT * FROM api_field_changes")), baseline)
        run, _ = await self.record(sample, HOUR_US + 100)
        change = (await self.rows("SELECT * FROM api_field_changes ORDER BY observed_at"))[-1]
        self.assertEqual(len(unpack(change["patch_z"])[0]), 1)
        self.assertEqual(change["source_run_id"], run)
        self.store = FamilyStore(self.path)
        await self.store.initialize()
        await self.record(sample, DAY + 100)
        change = (await self.rows("SELECT * FROM api_field_changes ORDER BY observed_at"))[-1]
        self.assertEqual(len(unpack(change["patch_z"])[0]), 2)
        self.assertEqual(unpack(change["patch_z"])[1], [])

    async def test_missing_null_empty_and_partial_arrays_keep_distinct_evidence(self):
        sample = fixture("player_4.json")
        sample["body"]["future"] = {"value": 1, "null": None}
        await self.record(sample)
        sample["body"]["future"] = []
        sample["body"]["troops"] = [{"name": "Malformed"}]
        _, result = await self.record(sample, DAY + 100)
        self.assertEqual(result["failed"], 1)
        stored = await self.store.call(read_fields, sample["kind"], sample["scope"])
        self.assertEqual(stored["/future"], [])
        self.assertIn("/future/value", stored)  # Partial data cannot establish removal.
        sample["body"]["troops"] = []
        await self.record(sample, 2 * DAY + 100)
        stored = await self.store.call(read_fields, sample["kind"], sample["scope"])
        self.assertNotIn("/future/value", stored)
        self.assertNotIn("/future/null", stored)

    async def test_future_war_and_group_fields_never_reduce_completeness(self):
        for name in ("current_war_inWar.json", "cwl_war_1.json", "cwl_group_1.json"):
            sample = fixture(name)
            body = sample["body"]
            body["future"] = {"optional": [1, None]}
            if sample["kind"] == "cwl_group":
                body["league"] = {"id": 123, "name": "Synthetic league", "future": {"value": 1}}
                body["clans"][0]["members"][0]["future"] = 1
            else:
                body["clan"]["future"] = 1
                body["clan"]["badgeUrls"]["future"] = {"value": 2}
                body["clan"]["members"][0]["future"] = 3
                attacks = next((row["attacks"] for row in body["clan"]["members"] if row.get("attacks")), [])
                if attacks:
                    attacks[0]["future"] = 4
            _, result = await self.record(sample)
            self.assertEqual(result["failed"], 0, name)
            stored = await self.store.call(read_fields, sample["kind"], sample["scope"])
            self.assertEqual(stored["/future/optional/1"], None)

    async def test_future_war_log_fields_survive_both_single_wars_and_weekly_summaries(self):
        sample = fixture("war_log_1.json")
        sample["body"]["future"] = {"page": 1}
        for row in sample["body"]["items"]:
            row["future"] = [None, []]
            row["clan"]["future"] = {"value": 1}
            row["opponent"]["future"] = {"value": 2}
            row["opponent"]["badgeUrls"]["future"] = [3]
        _, result = await self.record(sample)
        self.assertEqual(result["failed"], 0)
        stored = await self.store.call(read_fields, sample["kind"], sample["scope"])
        self.assertEqual(stored, {path: value for path, value, _ in leaves(sample["body"])})

    async def test_failed_transport_and_invalid_known_fields_leave_leaf_history_unchanged(self):
        sample = fixture("player_4.json")
        await self.record(sample)
        baseline = await self.rows("SELECT * FROM api_field_changes")
        for index, alteration in enumerate(({"trophies": -1}, {"currentLeagueSeasonId": "bad"},
                                             {"currentLeagueGroupTag": "bad"}), 1):
            broken = deepcopy(sample)
            broken["body"].update(alteration)
            _, result = await self.record(broken, index * HOUR_US)
            self.assertEqual(result["failed"], 1)
        await self.record(sample, 4 * HOUR_US, status=503)
        self.assertEqual(baseline, await self.rows("SELECT * FROM api_field_changes"))

    async def test_field_failure_rolls_back_typed_war_and_checkpoint(self):
        sample = fixture("cwl_war_1.json")
        with patch("elbow_helper.data.store.wars.collect_fields", side_effect=ValueError("bad_leaf")):
            _, result = await self.record(sample)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(await self.rows("SELECT * FROM wars"), [])
        self.assertEqual(await self.rows("SELECT * FROM api_field_changes"), [])


    async def test_path_escaping_array_reorder_type_changes_and_out_of_order(self):
        sample = fixture("clan_1.json")
        await self.record(sample)
        history = await self.rows("SELECT * FROM api_field_changes")
        sample["body"]["memberList"].reverse()
        await self.record(sample, HOUR_US)
        self.assertEqual(history, await self.rows("SELECT * FROM api_field_changes"))
        async def write(at, payload):
            run = await self.store.begin_run("test", "test", [], at)
            def save(c):
                with sqlite_transaction(c):
                    collect_fields(c, "player_profile", "test", payload, run, at)
            await self.store.call(save)
            await self.store.finish(run)
        await write(1, {"slash/key~": [True]})
        await write(DAY + 2, {"slash/key~": [1]})
        stored = await self.store.call(read_fields, "player_profile", "test")
        self.assertEqual(stored, {"/slash~1key~0/0": 1})
        self.assertIs(type(stored["/slash~1key~0/0"]), int)
        with self.assertRaises(ValueError):
            await write(1, {"slash/key~": []})
        with self.assertRaises(ValueError):
            await write(2 * DAY + 3, {"invalid": float("nan")})

    async def test_multiple_pages_merge_one_run_patch_including_removed_and_restored_leaves(self):
        run = await self.store.begin_run("test", "pages", [], 100)
        def pages(c):
            with sqlite_transaction(c):
                for at, payload in enumerate(({"removed": 1, "restored": 2}, {"restored": 3},
                                               {"removed": 4}, {"removed": 5, "restored": None}), 100):
                    collect_fields(c, "pages", "synthetic", payload, run, at)
        await self.store.call(pages)
        self.assertEqual(await self.store.call(read_fields, "pages", "synthetic"), {"/removed": 5, "/restored": None})
        patches = await self.rows("SELECT * FROM api_field_changes WHERE source_run_id=?", (run,))
        self.assertEqual(len(patches), 1)
        await self.store.finish(run)
