"""Profiles features ask the collectors for: served, fetched once, stored as observations, failures typed."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.data.collectors import FamilyCollectors, Profile
from elbow_helper.data.store import FamilyStore
from elbow_helper.data.store.clans import collect_clans
from elbow_helper.data.store.player_projection import HOUR_US
from elbow_helper.data.store.players import due_accounts
from elbow_helper.infrastructure.clash import ClashResponse

from .test_family_store import CLANS, clan_response, player, response
from .test_players import profile


DEMAND = "elbow_helper.data.collectors.demand.now_us"


class ProfileDemandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.store = FamilyStore(Path(self.temporary.name) / "family.sqlite3")
        await self.store.initialize()
        self.client = SimpleNamespace(configured=True, get=AsyncMock())
        self.collectors = FamilyCollectors(SimpleNamespace(boot_complete=asyncio.Event()), self.store, self.client, CLANS)
        self.profiles = self.collectors.profiles

    async def asyncTearDown(self):
        # Finish background writes before the temporary database is removed.
        await self.stored()

    async def rows(self, sql, args=()):
        return await self.store.call(lambda c: [dict(row) for row in c.execute(sql, args)])

    async def stored(self):
        # Wait for the background writer, as the collectors do when they close.
        if self.profiles._writer is not None:
            await self.profiles._writer

    async def scheduled_pass(self, at):
        specs = [(kind, clan.tag) for clan in CLANS for kind in ("clan_profile", "clan_roster")]
        run = await self.store.begin_run("clans", "family_clans", specs, at)
        await self.store.call(collect_clans, run, CLANS, [clan_response(0, [player()]), clan_response(1, [])], at)
        await self.store.finish(run)
        self.client.get.side_effect = [response(profile())]
        with patch("elbow_helper.data.collectors.service.now_us", return_value=at), \
                patch("elbow_helper.data.store.now_us", return_value=at), patch(DEMAND, return_value=at):
            await self.collectors.collect_players()
        self.client.get.reset_mock(side_effect=True)

    async def test_a_fresh_scheduled_profile_is_served_without_a_fetch(self):
        await self.scheduled_pass(100)
        with patch(DEMAND, return_value=100 + 30 * 60 * 1_000_000):
            result = await self.profiles.player("#P0", max_age=60 * 60)
        self.client.get.assert_not_awaited()
        self.assertTrue(result.ok)
        self.assertFalse(result.fetched)
        self.assertEqual((result.payload["tag"], result.observed_at, result.status), ("#P0", 100, 200))
        # Each caller owns its copy.
        result.payload["name"] = "Changed"
        with patch(DEMAND, return_value=200):
            self.assertNotEqual((await self.profiles.player("#P0")).payload["name"], "Changed")

    async def test_a_stale_profile_is_fetched_once_and_then_served(self):
        await self.scheduled_pass(100)
        later = 100 + 2 * HOUR_US
        renamed = {**profile(), "name": "Renamed"}
        self.client.get.return_value = response(renamed)
        with patch(DEMAND, return_value=later):
            first = await self.profiles.player("#P0", max_age=60 * 60)
            second = await self.profiles.player("#P0", max_age=60 * 60)
        self.client.get.assert_awaited_once()
        self.assertEqual(self.client.get.await_args.args, ("/players/%23P0",))
        self.assertTrue(self.client.get.await_args.kwargs["fresh"])
        self.assertTrue(first.fetched)
        self.assertFalse(second.fetched)
        self.assertEqual((first.payload["name"], second.payload["name"]), ("Renamed", "Renamed"))
        self.assertEqual((first.observed_at, second.observed_at), (later, later))
        # Zero always asks Clash again.
        with patch(DEMAND, return_value=later):
            await self.profiles.player("#P0", max_age=0)
        self.assertEqual(self.client.get.await_count, 2)

    async def test_concurrent_asks_for_one_tag_share_one_fetch(self):
        release = asyncio.Event()

        async def slow(path, **options):
            await release.wait()
            return response(profile("#Q2"))

        self.client.get.side_effect = slow
        asks = [asyncio.create_task(self.profiles.player("#Q2", attempts=2)) for _ in range(3)]
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(*asks)
        self.client.get.assert_awaited_once()
        self.assertEqual(self.client.get.await_args.kwargs, {"fresh": True, "attempts": 2})
        self.assertTrue(all(result.ok and result.fetched for result in results))
        self.assertEqual(len({id(result.payload) for result in results}), 3)
        await self.stored()

    async def test_a_cancelled_caller_leaves_the_shared_fetch_for_the_others(self):
        release = asyncio.Event()

        async def slow(path, **options):
            await release.wait()
            return response(profile("#Q2"))

        self.client.get.side_effect = slow
        leaving = asyncio.create_task(self.profiles.player("#Q2"))
        staying = asyncio.create_task(self.profiles.player("#Q2"))
        await asyncio.sleep(0)
        leaving.cancel()
        release.set()
        self.assertTrue((await staying).ok)
        with self.assertRaises(asyncio.CancelledError):
            await leaving
        self.client.get.assert_awaited_once()
        await self.stored()

    async def test_a_fetch_every_caller_gave_up_on_is_cancelled(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def never(path, **options):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.client.get.side_effect = never
        asks = [asyncio.create_task(self.profiles.player("#Q2")) for _ in range(2)]
        await entered.wait()
        for ask in asks:
            ask.cancel()
        await asyncio.gather(*asks, return_exceptions=True)
        await asyncio.wait_for(cancelled.wait(), 1)
        self.assertEqual(self.profiles._flights, {})
        self.assertEqual(await self.rows("SELECT * FROM collection_runs"), [])

    async def test_an_outside_account_is_stored_as_an_observation_only(self):
        self.client.get.return_value = response(profile("#Q2"))
        result = await self.profiles.player("#Q2")
        self.assertTrue(result.ok)
        await self.stored()
        account, = await self.rows("SELECT * FROM accounts WHERE player_tag='#Q2'")
        self.assertEqual(account["tracking_status"], "observed")
        self.assertIsNotNone(account["last_profile_at"])
        self.assertEqual(account["current_name"], profile()["name"])
        self.assertEqual(await self.rows("SELECT * FROM clan_membership WHERE account_id=?", (account["id"],)), [])
        self.assertEqual(await self.rows("SELECT * FROM account_links WHERE account_id=?", (account["id"],)), [])
        self.assertTrue(await self.rows("SELECT 1 FROM player_observations WHERE account_id=?", (account["id"],)))
        run, = await self.rows("SELECT * FROM collection_runs WHERE collector='player_demand'")
        self.assertEqual((run["status"], run["succeeded"], run["failed"]), ("ok", 1, 0))
        # No collector schedules it.
        self.assertEqual(await self.store.call(due_accounts, 10 * HOUR_US + account["last_profile_at"]), [])

    async def test_an_account_a_scheduled_pass_holds_is_left_to_that_pass(self):
        await self.scheduled_pass(100)
        account_id = (await self.rows("SELECT id FROM accounts WHERE player_tag='#P0'"))[0]["id"]
        scheduled = await self.store.begin_run("players", "family_and_linked_accounts", [("player_profile", "#P0")], 200)
        self.client.get.return_value = response({**profile(), "name": "Asked"})
        self.assertEqual((await self.profiles.player("#P0", max_age=0)).payload["name"], "Asked")
        await self.stored()
        state, = await self.rows("SELECT * FROM player_collection_state WHERE account_id=?", (account_id,))
        self.assertEqual((state["active_run_id"], state["queue_status"]), (scheduled, "pending"))
        self.assertNotEqual((await self.rows("SELECT current_name FROM accounts WHERE id=?", (account_id,)))[0]["current_name"],
                            "Asked")

    async def test_outside_clans_are_stored_and_family_clans_left_to_their_pass(self):
        outside = {"tag": "#2PQ", "name": "Outside", "warLeague": {"name": "Synthetic League"},
                   "memberList": [player("#Q2")]}
        family = clan_response(0, [player()])
        self.client.get.side_effect = [response(outside), family]
        self.assertEqual((await self.profiles.clan("#2PQ")).payload["name"], "Outside")
        self.assertEqual((await self.profiles.clan(CLANS[0].tag)).payload["name"], CLANS[0].name)
        await self.stored()
        self.assertEqual([row["scope_id"] for row in await self.rows("SELECT scope_id FROM api_records")], ["#2PQ"])
        self.assertEqual(await self.rows("SELECT * FROM clans"), [])
        self.assertEqual(await self.rows("SELECT * FROM accounts"), [])
        run, = await self.rows("SELECT * FROM collection_runs WHERE collector='clan_demand'")
        self.assertEqual((run["status"], run["succeeded"]), ("ok", 1))

    async def test_scheduled_clan_profiles_are_served(self):
        self.client.get.side_effect = [clan_response(0, [player()]), clan_response(1, [])]
        await self.collectors.collect_clans()
        self.client.get.reset_mock(side_effect=True)
        result = await self.profiles.clan(CLANS[1].tag, max_age=15 * 60)
        self.assertEqual(result.payload["name"], CLANS[1].name)
        self.client.get.assert_not_awaited()

    async def test_failures_come_back_typed_and_store_nothing(self):
        cases = (
            (response({"reason": "notFound"}, 404), "not_found", 404),
            (response({"reason": "maintenance"}, 503), "http_error", 503),
            (ClashResponse(None, None, {}, 3, None, TimeoutError()), "network_error", None),
            (ClashResponse(200, None, {}, 1, 2, ValueError("not json")), "network_error", 200),
            (response(["not", "a", "profile"]), "invalid_payload", 200),
            (RuntimeError("unexpected"), "collector_error", None),
        )
        for reply, failure, status in cases:
            with self.subTest(failure=failure, status=status):
                self.client.get.reset_mock()
                self.client.get.side_effect = [reply]
                result = await self.profiles.player("#Q2")
                self.assertIsInstance(result, Profile)
                self.assertEqual((result.ok, result.failure, result.status, result.payload), (False, failure, status, None))
        self.assertEqual((await self.profiles.player("not a tag")).failure, "invalid_tag")
        self.client.configured = False
        self.client.get.reset_mock()
        self.assertEqual((await self.profiles.player("#Q2")).failure, "not_configured")
        self.client.get.assert_not_awaited()
        await self.stored()
        self.assertEqual(await self.rows("SELECT * FROM accounts"), [])
        self.assertEqual(await self.rows("SELECT * FROM collection_runs"), [])

    async def test_a_store_failure_is_recorded_and_the_others_still_stored(self):
        self.client.get.side_effect = lambda path, **options: response(profile("#" + path.rsplit("%23", 1)[1]))
        with patch("elbow_helper.data.collectors.demand.collect_requested",
                   side_effect=[ValueError("synthetic"), True]) as collect:
            await asyncio.gather(self.profiles.player("#Q2"), self.profiles.player("#Q8"))
            await self.stored()
        self.assertEqual(collect.call_count, 2)
        self.assertTrue((await self.profiles.player("#Q2")).ok)

    async def test_closing_ends_open_fetches_without_raising_into_features(self):
        entered = asyncio.Event()

        async def never(path, **options):
            entered.set()
            await asyncio.Event().wait()

        self.client.get.side_effect = never
        ask = asyncio.create_task(self.profiles.player("#Q2"))
        await entered.wait()
        await self.collectors.cog_unload()
        self.assertEqual((await ask).failure, "collector_error")


if __name__ == "__main__":
    unittest.main()
