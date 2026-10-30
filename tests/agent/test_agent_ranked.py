from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import unittest

from agent.capabilities.member_lifecycle.test_agent_member_lifecycle import AgentMemberLifecycleTests
from features.test_ranked_stats import observation, battle, save_log
from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.access import AgentAccessLost
from elbow_helper.agent.datasets.ranked import read_ranked_stats
from elbow_helper.features.ranked_stats.queries import RankedQueries
from elbow_helper.features.ranked_stats.store import RankedStore
from elbow_helper.agent.plan.results import model_result


class AgentRankedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = AgentMemberLifecycleTests()
        fixture.setUp()
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = RankedStore(Path(self.temp.name) / "ranked.sqlite3")
        self.store.initialize()
        self.queries = RankedQueries(self.store)
        self.context = replace(fixture.context, ranked_queries=self.queries)
        self.arguments = {"member_ids": [42], "after": "2026-09-01", "before": "2026-09-02"}

    async def test_historical_read_works_without_current_clash_configuration(self):
        save_log(self.store, [battle(), battle(minute=2)])
        result = await read_ranked_stats(self.context, self.arguments)
        self.assertEqual(result["accounts"][0]["attacks_used"], 2)
        self.assertEqual(result["accounts"][0]["daily"][0]["attacks_used"], 2)

    async def test_clan_codes_account_normalization_and_exclusive_end(self):
        code, clan = next(iter(CLANS.items()))
        self.store.append([observation(clan=clan.tag), observation(day=2, minute=0)])
        result = await read_ranked_stats(self.context, {**self.arguments, "member_ids": [], "clan_tags": [code]})
        self.assertEqual(result["accounts"][0]["sample_count"], 1)
        result = await read_ranked_stats(self.context, {**self.arguments, "member_ids": [], "account_tags": ["p0"]})
        self.assertEqual(result["accounts"][0]["player_tag"], "#P0")

    async def test_invalid_scope_window_and_paging_are_rejected_before_querying(self):
        context = replace(self.context, ranked_queries=Mock())
        for overrides in ({"account_tags": ["#P0"]}, {"after": "2026-09-02"},
                          {"after": "invalid"},
                          {"member_ids": [True]}, {"member_ids": []}, {"limit": 26},
                          {"offset": -1}):
            with self.subTest(overrides=overrides):
                result = await read_ranked_stats(context, {**self.arguments, **overrides})
                self.assertIn("error", result)
        context.ranked_queries.window.assert_not_called()

    async def test_lost_agent_access_prevents_historical_disclosure(self):
        self.context.member.roles.clear()
        with self.assertRaises(AgentAccessLost):
            await read_ranked_stats(self.context, self.arguments)

    async def test_missing_feature_and_unobserved_account_are_distinct(self):
        missing = await read_ranked_stats(replace(self.context, ranked_queries=None), self.arguments)
        self.assertIn("error", missing)
        empty = await read_ranked_stats(self.context, self.arguments)
        self.assertEqual(empty["accounts"], [])
        self.assertEqual(empty["unobserved_selections"], [42])

    async def test_empty_member_history_reads_linked_live_accounts_automatically(self):
        client = SimpleNamespace(configured=True, get=AsyncMock(return_value=SimpleNamespace(
            ok=True, status=200, payload={"items": [battle()]},
        )))
        links = Mock(get_links_for_members=Mock(return_value={42: [{"player_tag": "p0"}]}))
        context = replace(self.context, bot=SimpleNamespace(clash_client=client), account_links=links)
        result = model_result(await read_ranked_stats(context, self.arguments))
        self.assertEqual(result["flags"]["status"], "partial")
        self.assertEqual(result["history_coverage"]["status"], "empty")
        self.assertIn("empty_ranked_history", result["flags"]["limits"])
        self.assertEqual(client.get.await_args.args[0], "/players/%23P0/battlelog")
        live = result["live_battle_logs"]
        self.assertFalse(live["covers_requested_window"])
        self.assertEqual(live["result"]["items"][0]["data"]["items"][0]["battleType"], "RANKED")

    async def test_each_incomplete_indicator_marks_result_partial_and_reads_live(self):
        complete_row = {"player_tag": "#P0", "battle_records": 1, "counts_complete": True,
                        "failed_battle_log_polls": 0, "first_observed_at": "2026-09-01",
                        "last_observed_at": "2026-09-02"}
        for changes, reason in (({"counts_complete": False}, "incomplete_ranked_counts"),
                                ({"failed_battle_log_polls": 1}, "failed_battle_log_polls"),
                                ({"first_observed_at": "2026-09-01T01:00:00Z"}, "shorter_observation_window"),
                                ({"last_observed_at": "2026-09-01T23:00:00Z"}, "shorter_observation_window")):
            with self.subTest(changes=changes):
                query = Mock(window=Mock(return_value={"accounts": [{**complete_row, **changes}],
                                                       "complete": True, "coverage_complete": True}))
                with patch("elbow_helper.agent.datasets.ranked.read_clash", AsyncMock(
                    return_value={"items": [], "observed_at": "synthetic"},
                )) as read:
                    result = model_result(await read_ranked_stats(
                        replace(self.context, ranked_queries=query), self.arguments))
                self.assertIn(reason, result["flags"]["limits"])
                self.assertEqual(result["flags"]["status"], "partial")
                read.assert_awaited_once()

    async def test_complete_history_does_not_trigger_live_read(self):
        query = Mock(window=Mock(return_value={"accounts": [{"player_tag": "#P0", "battle_records": 1,
            "counts_complete": True, "first_observed_at": "2026-09-01", "last_observed_at": "2026-09-02"}],
            "complete": True, "coverage_complete": True}))
        with patch("elbow_helper.agent.datasets.ranked.read_clash", AsyncMock()) as read:
            result = model_result(await read_ranked_stats(replace(self.context, ranked_queries=query), self.arguments))
        self.assertEqual(result["flags"]["status"], "complete")
        read.assert_not_awaited()

    async def test_live_unavailability_keeps_stored_counts_partial(self):
        save_log(self.store, [battle()])
        result = model_result(await read_ranked_stats(self.context, self.arguments))
        self.assertEqual(result["accounts"][0]["attacks_used"], 1)
        self.assertEqual(result["flags"]["status"], "partial")
        self.assertIn("error", result["live_battle_logs"]["result"])

    async def test_empty_clan_history_does_not_fan_out_to_every_member_log(self):
        with patch("elbow_helper.agent.datasets.ranked.read_clash", AsyncMock()) as read:
            result = await read_ranked_stats(self.context, {**self.arguments, "member_ids": [], "clan_tags": ["#Q0"]})
        read.assert_not_awaited()
        self.assertIn("skipped", result["live_battle_logs"]["result"])
        self.assertEqual(result["history_coverage"]["status"], "empty")
