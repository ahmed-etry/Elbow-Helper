from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
import unittest

from features.agent.capabilities.member_lifecycle.test_agent_member_lifecycle import AgentMemberLifecycleTests
from features.test_ranked_stats import observation, stamp
from elbow_helper.configuration.clans import CLANS
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.datasets.ranked import read_ranked_stats
from elbow_helper.features.ranked_stats.queries import RankedQueries
from elbow_helper.features.ranked_stats.store import RankedStore


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
        self.store.append([observation(wins=10), observation(wins=15, minute=5)])
        result = await read_ranked_stats(self.context, self.arguments)
        self.assertEqual(result["accounts"][0]["attack_wins_observed"], 5)
        self.assertIsNone(result["accounts"][0]["attacks_used"])

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
                          {"after": "2025-01-01"}, {"after": "invalid"},
                          {"member_ids": [True]}, {"member_ids": []}, {"limit": 26},
                          {"offset": -1}, {"before": "2026-11-30T00:00:01Z"}):
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
