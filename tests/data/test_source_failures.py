"""Source polls separate real failures from missing, unavailable and unusual-but-valid answers."""

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.data.collectors.service import FamilyCollectors
from elbow_helper.data.collectors.sources import collect_sources
from elbow_helper.data.store.projections import canonical_json
from elbow_helper.data.store.sources import DAY, due_sources
from elbow_helper.infrastructure.persistence.sqlite import sqlite_transaction

from .test_family_store import CLANS, response
from .test_sources import BASE, SourceTests, sample


SERVICE = "elbow_helper.data.collectors.service"
SEASON = 1792990800


class BattleLogShapeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = SourceTests.asyncSetUp
    rows = SourceTests.rows
    record = SourceTests.record
    assert_leaves = SourceTests.assert_leaves

    def battles(self, count=3):
        data = sample("battle_log")
        data["body"]["items"] = data["body"]["items"][:count]
        return data

    async def test_battles_without_a_usable_opponent_tag_are_kept_and_stable(self):
        data = self.battles()
        items = data["body"]["items"]
        del items[0]["opponentPlayerTag"]
        del items[1]["opponentPlayerTag"]
        del items[1]["opponentName"]
        items[2]["opponentPlayerTag"] = "not a tag"
        _, report = await self.record(data)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(len(await self.rows("SELECT * FROM family_battles")), 3)
        await self.assert_leaves(data)
        before = await self.rows("SELECT * FROM family_battles")
        _, report = await self.record(data, BASE + DAY)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(before, await self.rows("SELECT * FROM family_battles"))
        self.assertEqual(await self.rows("SELECT * FROM battle_changes"), [])

    async def test_tagless_battles_in_the_same_second_stay_apart_by_opponent_name(self):
        data = self.battles(1)
        first = data["body"]["items"][0]
        del first["opponentPlayerTag"]
        second = deepcopy(first)
        second["opponentName"] = "Synthetic name other"
        data["body"]["items"].append(second)
        _, report = await self.record(data)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(len(await self.rows("SELECT * FROM family_battles")), 2)
        await self.assert_leaves(data)

    async def test_a_battle_repeated_word_for_word_counts_once(self):
        data = self.battles(2)
        data["body"]["items"].append(deepcopy(data["body"]["items"][0]))
        del data["body"]["items"][-1]["opponentPlayerTag"]
        data["body"]["items"][0].pop("opponentPlayerTag")
        _, report = await self.record(data)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(len(await self.rows("SELECT * FROM family_battles")), 2)

    async def test_battles_without_a_time_or_direction_still_fail_and_keep_nothing(self):
        cases = ((("battleTimestamp", None), "missing_time"), (("battleTimestamp", "yesterday"), "missing_time"),
                 (("attack", "yes"), "bad_direction"), (("attack", None), "bad_direction"))
        for (field, value), reason in cases:
            with self.subTest(field=field, value=value):
                data = self.battles()
                data["body"]["items"][1][field] = value
                run, report = await self.record(data)
                self.assertEqual((report["failed"], report["reasons"]), (1, {f"invalid_payload:{reason}": 1}))
        data = self.battles()
        del data["body"]["items"][1]["attack"]
        data["body"]["items"][2]["future"] = {"secret": "payload text"}
        data["body"]["items"].append({**data["body"]["items"][2], "future": "conflict"})
        _, report = await self.record(data)
        self.assertEqual(report["reasons"], {"invalid_payload:bad_direction": 1})
        self.assertEqual(await self.rows("SELECT * FROM family_battles"), [])
        items = await self.rows("SELECT error_code,http_status FROM collection_items")
        self.assertEqual({(row["error_code"], row["http_status"]) for row in items},
                         {("invalid_payload:missing_time", 200), ("invalid_payload:bad_direction", 200)})

    async def test_conflicting_identity_and_unexpected_errors_name_a_safe_reason(self):
        data = self.battles(1)
        data["body"]["items"].append({**data["body"]["items"][0], "future": "conflict"})
        _, report = await self.record(data)
        self.assertEqual(report["reasons"], {"invalid_payload:duplicate_source_identity": 1})
        data = self.battles(1)
        data["body"]["future"] = float("nan")
        _, report = await self.record(data, BASE + DAY)
        self.assertEqual(list(report["reasons"]), ["invalid_payload:valueerror"])
        self.assertNotIn("nan", str(await self.rows("SELECT error_code FROM collection_items")).lower().replace("invalid_payload", ""))


class RankedGroupOutcomeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = SourceTests.asyncSetUp
    rows = SourceTests.rows
    record = SourceTests.record
    seed_account = SourceTests.seed_account

    async def references(self, *rows):
        run = await self.seed_account()

        def insert(c):
            with sqlite_transaction(c):
                for offset, row in enumerate(rows):
                    c.execute("INSERT INTO player_ranked_references VALUES (1,?,?,?,?,?,?)", (BASE + offset, *row, run))
        await self.store.call(insert)

    async def test_no_group_is_never_requested(self):
        await self.references(("#0", SEASON, "#0", SEASON - 7 * 86400), (None, None, None, None),
                              ("#PP2", SEASON, "#0", SEASON - 7 * 86400))
        jobs = await self.store.call(due_sources, "ranked_groups", CLANS, BASE)
        self.assertEqual([job["scope_id"] for job in jobs], [canonical_json(["#P0", "#PP2", SEASON])])

    async def test_account_with_only_no_group_has_nothing_to_poll(self):
        await self.references(("#0", SEASON, "#0", SEASON - 7 * 86400))
        self.assertEqual(await self.store.call(due_sources, "ranked_groups", CLANS, BASE), [])

    async def test_each_outcome_stays_distinct_and_only_failures_count_as_failed(self):
        data = sample("ranked_group")
        scopes = {name: canonical_json(["#P0", f"#PP{index}", SEASON]) for index, name in enumerate(
            ("worked", "missing", "gone", "broken"), start=1)}
        cases = (("worked", 200, True), ("missing", 404, True), ("gone", 500, False), ("broken", 500, True))
        reports = {}
        for index, (name, status, current) in enumerate(cases):
            data["scope"] = scopes[name]
            _, reports[name] = await self.record(data, BASE + index * DAY, status=status, current=current)
        self.assertEqual({name: report["failed"] for name, report in reports.items()},
                         {"worked": 0, "missing": 0, "gone": 0, "broken": 1})
        items = {row["scope_id"]: row for row in await self.rows("SELECT * FROM collection_items")}
        self.assertNotIn(scopes["worked"], items)
        self.assertEqual((items[scopes["missing"]]["status"], items[scopes["missing"]]["error_code"],
                          items[scopes["missing"]]["http_status"]), ("empty", "group_not_found", 404))
        self.assertEqual((items[scopes["gone"]]["status"], items[scopes["gone"]]["error_code"],
                          items[scopes["gone"]]["http_status"]), ("unavailable", "old_season_unavailable", 500))
        self.assertEqual((items[scopes["broken"]]["status"], items[scopes["broken"]]["error_code"]), ("failed", "fetch_failed"))
        runs = await self.rows("SELECT succeeded,empty,failed FROM collection_runs ORDER BY id")
        self.assertEqual([(row["succeeded"], row["empty"], row["failed"]) for row in runs],
                         [(1, 0, 0), (0, 1, 0), (0, 1, 0), (0, 0, 1)])

    async def test_missing_group_retires_with_a_finished_season_but_a_current_one_is_asked_again(self):
        data = sample("ranked_group")
        for name, current in (("current", True), ("finished", False)):
            data["scope"] = canonical_json(["#P0", f"#PP{name}", SEASON])
            await self.record(data, status=404, current=current)
        state = {row["scope_id"]: row for row in await self.rows("SELECT * FROM family_api_state")}
        self.assertEqual([state[canonical_json(["#P0", f"#PP{name}", SEASON])]["enabled"] for name in ("current", "finished")], [1, 0])
        self.assertEqual({row["last_status"] for row in state.values()}, {"group_not_found"})


class SourceWarningTests(unittest.IsolatedAsyncioTestCase):
    rows = SourceTests.rows
    seed_account = SourceTests.seed_account

    async def asyncSetUp(self):
        await SourceTests.asyncSetUp(self)
        await self.seed_account()
        self.client = SimpleNamespace(get=AsyncMock())
        self.owner = FamilyCollectors(SimpleNamespace(boot_complete=asyncio.Event()), self.store, self.client, CLANS)

    async def poll(self, collector, at):
        def reset(c):
            with sqlite_transaction(c):
                c.execute("UPDATE family_api_state SET next_due_at=?,last_attempt_at=NULL", (at,))
        await self.store.call(reset)
        with patch("elbow_helper.data.collectors.sources.now_us", return_value=at), patch(f"{SERVICE}.now_us", return_value=at):
            return await collect_sources(self.owner, collector)

    async def test_a_failing_collector_warns_once_an_hour_with_the_runs_it_held_back(self):
        minute = 60 * 1_000_000
        self.client.get.return_value = response(None, 500)
        with self.assertLogs(SERVICE, level="WARNING") as first:
            await self.poll("battle_logs", BASE)
        self.assertEqual(len(first.output), 1)
        self.assertNotIn("more runs", first.output[0])
        self.assertIn("fetch_failed=1", first.output[0])
        with self.assertNoLogs(SERVICE, level="WARNING"):
            await self.poll("battle_logs", BASE + 10 * minute)
            await self.poll("battle_logs", BASE + 20 * minute)
        with self.assertLogs(SERVICE, level="WARNING") as later:
            report = await self.poll("battle_logs", BASE + 61 * minute)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(len(later.output), 1)
        self.assertIn("2 more runs and 2 failed items since the last warning: fetch_failed=2", later.output[0])
        with self.assertLogs(SERVICE, level="WARNING") as other:
            await self.poll("league_history", BASE + 62 * minute)
        self.assertEqual(len(other.output), 1)
        self.assertEqual(len(await self.rows("SELECT * FROM collection_items WHERE status='failed'")), 5)

    async def test_missing_and_unavailable_groups_do_not_warn_and_no_group_is_not_asked(self):
        run = (await self.rows("SELECT id FROM collection_runs"))[0]["id"]

        def seed(c):
            with sqlite_transaction(c):
                c.execute("INSERT INTO player_ranked_references VALUES (1,?,'#PP9',4000000000,NULL,NULL,?)", (BASE, run))
                c.execute("INSERT INTO player_ranked_references VALUES (1,?,'#0',4000000000,'#0',123,?)", (BASE + 1, run))
                c.execute("INSERT INTO player_ranked_references VALUES (1,?,'#PP9',4000000000,'#PP8',123,?)", (BASE + 2, run))
        await self.store.call(seed)
        self.client.get.return_value = response({"reason": "notFound"}, 404)
        with self.assertNoLogs(SERVICE, level="WARNING"):
            report = await collect_sources(self.owner, "ranked_groups")
        paths = [call.args[0] for call in self.client.get.await_args_list]
        self.assertTrue(paths)
        self.assertFalse(any("%230/" in path for path in paths))
        self.assertEqual((report["failed"], report["empty"]), (0, len(paths)))
        self.assertEqual(json.loads((await self.rows("SELECT scope_id FROM collection_items LIMIT 1"))[0]["scope_id"])[0], "#P0")
