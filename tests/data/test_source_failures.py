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

    async def test_a_battle_log_answering_500_every_time_warns_once_and_recovery_logs_once(self):
        hour = 3600 * 1_000_000
        self.client.get.return_value = response(None, 500)
        with self.assertLogs(SERVICE, level="WARNING") as first:
            report = await self.poll("battle_logs", BASE)
        self.assertEqual((report["failed"], report["status"]), (1, "failed"))
        self.assertEqual(len(first.output), 1)
        self.assertIn("battle_log #P0 (HTTP 500)", first.output[0])
        with self.assertNoLogs(SERVICE, level="INFO"):
            for later in (1, 2, 3):
                report = await self.poll("battle_logs", BASE + later * hour)
                self.assertEqual((report["failed"], report["empty"], report["status"]), (0, 1, "empty"))
        items = await self.rows("SELECT status,error_code,http_status FROM collection_items WHERE source_kind='battle_log' ORDER BY id")
        self.assertEqual([(row["status"], row["error_code"], row["http_status"]) for row in items],
                         [("failed", "fetch_failed", 500)] + [("unavailable", "log_unavailable", 500)] * 3)
        state = (await self.rows("SELECT * FROM family_api_state WHERE source_kind='battle_log'"))[0]
        self.assertEqual((state["enabled"], state["next_due_at"]), (1, BASE + 4 * hour))
        self.assertEqual(self.client.get.await_count, 4)
        self.client.get.return_value = response({"items": []})
        with self.assertLogs(SERVICE, level="INFO") as recovered:
            report = await self.poll("battle_logs", BASE + 4 * hour)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(len(recovered.output), 1)
        self.assertIn("recovered", recovered.output[0])
        self.assertIn("battle_log #P0 (HTTP 500)", recovered.output[0])
        with self.assertNoLogs(SERVICE, level="INFO"):
            await self.poll("battle_logs", BASE + 5 * hour)

    async def test_a_500_after_a_good_poll_is_a_failure_again(self):
        hour = 3600 * 1_000_000
        self.client.get.return_value = response({"items": []})
        await self.poll("battle_logs", BASE)
        self.client.get.return_value = response(None, 500)
        with self.assertLogs(SERVICE, level="WARNING") as logs:
            report = await self.poll("battle_logs", BASE + hour)
        self.assertEqual((report["failed"], len(logs.output)), (1, 1))

    async def test_only_battle_logs_treat_a_repeated_500_as_unavailable(self):
        self.client.get.return_value = response(None, 500)
        with self.assertLogs(SERVICE, level="WARNING") as logs:
            for index in range(3):
                report = await self.poll("league_history", BASE + index)
                self.assertEqual(report["failed"], 1)
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(len(await self.rows("SELECT * FROM collection_items WHERE status='failed'")), 3)

    async def test_a_one_off_failure_warns_once_and_recovery_logs_once(self):
        fail = True

        async def fetch(path, **options):
            if fail and path.startswith("/labels/clans"):
                raise TimeoutError("private detail")
            return response(sample("goldpass")["body"] if path.startswith("/goldpass") else sample("catalogue")["body"])
        self.client.get.side_effect = fetch
        with self.assertLogs(SERVICE, level="WARNING") as first:
            await self.poll("reference_data", BASE)
        self.assertEqual(len(first.output), 1)
        self.assertIn("catalogue /labels/clans (fetch_failed)", first.output[0])
        self.assertNotIn("private detail", first.output[0])
        fail = False
        with self.assertLogs(SERVICE, level="INFO") as second:
            report = await self.poll("reference_data", BASE + 1)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(len(second.output), 1)
        self.assertIn("recovered", second.output[0])
        with self.assertNoLogs(SERVICE, level="INFO"):
            await self.poll("reference_data", BASE + 2)

    async def test_repeated_failures_stay_quiet_but_a_new_item_or_a_new_kind_of_failure_warns(self):
        broken = {"/labels/clans": 503}

        async def fetch(path, **options):
            if path.split("?")[0] in broken:
                return response(None, broken[path.split("?")[0]])
            return response(sample("goldpass")["body"] if path.startswith("/goldpass") else sample("catalogue")["body"])
        self.client.get.side_effect = fetch
        with self.assertLogs(SERVICE, level="WARNING") as first:
            await self.poll("reference_data", BASE)
        self.assertEqual(len(first.output), 1)
        with self.assertNoLogs(SERVICE, level="WARNING"):
            await self.poll("reference_data", BASE + 1)
        broken["/labels/players"] = 503
        with self.assertLogs(SERVICE, level="WARNING") as new_item:
            await self.poll("reference_data", BASE + 2)
        self.assertEqual(len(new_item.output), 1)
        self.assertIn("/labels/players (HTTP 503)", new_item.output[0])
        self.assertNotIn("/labels/clans", new_item.output[0])
        broken["/labels/clans"] = 502
        with self.assertLogs(SERVICE, level="WARNING") as new_kind:
            await self.poll("reference_data", BASE + 3)
        self.assertEqual(len(new_kind.output), 1)
        self.assertIn("/labels/clans (HTTP 502)", new_kind.output[0])
        self.assertNotIn("/labels/players", new_kind.output[0])
        with self.assertNoLogs(SERVICE, level="WARNING"):
            await self.poll("reference_data", BASE + 4)
        broken.pop("/labels/players")
        with self.assertLogs(SERVICE, level="INFO") as one_recovered:
            await self.poll("reference_data", BASE + 5)
        self.assertEqual(len(one_recovered.output), 1)
        self.assertIn("/labels/players", one_recovered.output[0])
        self.assertNotIn("/labels/clans", one_recovered.output[0])

    async def test_the_same_failure_is_a_debug_line(self):
        self.client.get.return_value = response(None, 503)
        with self.assertLogs(SERVICE, level="WARNING"):
            await self.poll("league_history", BASE)
        with self.assertLogs(SERVICE, level="DEBUG") as logs:
            await self.poll("league_history", BASE + 1)
        self.assertEqual([record.levelname for record in logs.records], ["DEBUG"])
        self.assertIn("still failing", logs.output[0])

    async def test_a_failure_to_list_the_items_is_still_reported_once(self):
        self.client.get.return_value = response(None, 503)
        with patch(f"{SERVICE}.FamilyStore.problems", side_effect=OSError("unavailable")):
            with self.assertLogs(SERVICE, level="WARNING") as first:
                await self.poll("league_history", BASE)
            with self.assertNoLogs(SERVICE, level="WARNING"):
                await self.poll("league_history", BASE + 1)
        self.assertEqual(len(first.output), 1)
        self.assertIn("item_failure", first.output[0])

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
