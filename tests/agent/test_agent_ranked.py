"""Ranked reads over the family record: stored counts, coverage gaps and no live Clash reads."""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import sqlite3
import unittest

from agent.capabilities.member_lifecycle.test_agent_member_lifecycle import AgentMemberLifecycleTests
from data.test_ranked_import import BASE, SECOND, RankedFixture, battle
from elbow_helper.agent.access import AgentAccessLost
from elbow_helper.agent.reads import ranked
from elbow_helper.agent.reads.ranked import read_ranked
from elbow_helper.agent.plan.results import model_result
from elbow_helper.data.reads import RankedReads


def iso(value):
    return datetime.fromtimestamp(value / SECOND, timezone.utc).isoformat()


class RankedRecordFixture(RankedFixture):
    """A synthetic family record holding one linked account (#P0, member 100)."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        fixture = AgentMemberLifecycleTests()
        fixture.setUp()
        self.reads = RankedReads(self.store)
        self.context = replace(fixture.context, bot=SimpleNamespace(ranked_reads=self.reads))
        self.arguments = {"member_ids": [100], "after": iso(BASE - 3600 * SECOND), "before": iso(BASE + 3600 * SECOND)}


class AgentRankedTests(RankedRecordFixture, unittest.IsolatedAsyncioTestCase):
    async def test_stored_counts_come_from_copied_and_live_battles_without_clash(self):
        self.add_battle(battle(-10))
        await self.copy()
        await self.live({"items": [battle(-10), battle(60)]}, 100)
        with patch.object(ranked, "require_evidence_access", AsyncMock()), \
                patch("elbow_helper.agent.datasets.clash.read_clash", AsyncMock(side_effect=AssertionError("live read"))):
            result = await read_ranked(self.context, self.arguments)
        row = result["rows"][0]
        self.assertEqual((row["attacks_used"], row["battle_records"]), (2, 2))
        self.assertEqual(row["daily"][0], {"date": "2026-10-15", "attacks_used": 1, "defenses": 0})
        self.assertEqual(row["log_coverage"]["watched_from"], iso(BASE))
        self.assertEqual(result["source"], "family_record")
        self.assertNotIn("live_battle_logs", result)

    async def test_clan_codes_account_normalization_and_selection(self):
        await self.copy()
        result = await read_ranked(self.context, {**self.arguments, "member_ids": [], "account_tags": ["p0"]})
        self.assertEqual(result["rows"][0]["player_tag"], "#P0")
        result = await read_ranked(self.context, {**self.arguments, "member_ids": [], "clan_tags": ["#Q0"]})
        self.assertEqual((result["rows"], result["unobserved_selections"]), ([], ["#Q0"]))
        self.assertEqual(result["history_coverage"]["status"], "empty")

    async def test_invalid_scope_window_and_paging_are_rejected_before_reading(self):
        context = replace(self.context, bot=SimpleNamespace(ranked_reads=Mock()))
        for overrides in ({"account_tags": ["#P0"]}, {"after": iso(BASE + 3600 * SECOND)},
                          {"after": "invalid"}, {"member_ids": [True]}, {"member_ids": []},
                          {"limit": 501}, {"offset": -1}):
            with self.subTest(overrides=overrides):
                result = await read_ranked(context, {**self.arguments, **overrides})
                self.assertIn("error", result)
        context.bot.ranked_reads.window.assert_not_called()

    async def test_lost_agent_access_prevents_historical_disclosure(self):
        self.context.member.roles.clear()
        with self.assertRaises(AgentAccessLost):
            await read_ranked(self.context, self.arguments)

    async def test_missing_record_unreadable_record_and_unobserved_member_are_distinct(self):
        missing = await read_ranked(replace(self.context, bot=SimpleNamespace()), self.arguments)
        self.assertEqual(missing, {"error": "Ranked observations are not available."})
        failing = SimpleNamespace(ranked_reads=SimpleNamespace(window=AsyncMock(side_effect=sqlite3.OperationalError("x"))))
        self.assertEqual(await read_ranked(replace(self.context, bot=failing), self.arguments),
                         {"error": "Ranked observations could not be read."})
        empty = await read_ranked(self.context, {**self.arguments, "member_ids": [999]})
        self.assertEqual((empty["rows"], empty["unobserved_selections"]), ([], [999]))

    async def test_coverage_gaps_mark_the_result_partial_instead_of_reading_live(self):
        self.add_battle(battle(-10))
        await self.copy()
        await self.live({"items": []}, 100)
        await self.live(None, 120, status=0)
        await self.live({"items": []}, 140)
        result = model_result(await read_ranked(self.context, self.arguments))
        self.assertEqual(result["flags"]["status"], "partial")
        for reason in ("battle_log_before_first_read", "battle_log_between_watchers", "battle_log_log_not_read",
                       "failed_battle_log_reads", "shorter_observation_window"):
            self.assertIn(reason, result["history_coverage"]["reasons"])
        self.assertEqual(result["rows"][0]["attacks_used"], 1)

    async def test_a_fully_watched_window_is_not_marked_partial_by_coverage(self):
        self.poll(200)  # The old collector still read the log after the record's first read.
        await self.copy()
        await self.live({"items": [battle(30)]}, 100)
        arguments = {**self.arguments, "after": iso(BASE), "before": iso(BASE + 50 * SECOND)}
        result = await read_ranked(self.context, arguments)
        self.assertEqual(result["rows"][0]["log_coverage"]["gaps"], [])
        # Old and new: counts are never claimed complete, so the read still says partial.
        self.assertEqual(result["history_coverage"]["reasons"], ["incomplete_ranked_counts"])
