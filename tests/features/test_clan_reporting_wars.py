from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, call, patch

import discord

from elbow_helper.configuration.clans import CLAN_TAGS
from elbow_helper.features.clan_reporting.api import ClanReportingApiMixin
from elbow_helper.features.clan_reporting.helpers import ClanReportingHelperMixin
from elbow_helper.features.clan_reporting.state import load_state
from elbow_helper.features.clan_reporting.wars import ClanReportingWarMixin


class _SummaryManager(
    ClanReportingWarMixin, ClanReportingHelperMixin, ClanReportingApiMixin,
):
    pass


class MonthlyWarSummaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clans = ("BEH", "BE4", "BES")
        self.persisted_state = "{}"
        self.saved_states = []
        self.enterContext(patch(
            "elbow_helper.features.clan_reporting.state.read_json",
            side_effect=lambda _: json.loads(self.persisted_state),
        ))
        self.enterContext(patch(
            "elbow_helper.features.clan_reporting.state.write_json_atomic",
            side_effect=self.save_state,
        ))
        self.enterContext(patch(
            "elbow_helper.features.clan_reporting.state.CLANS", self.clans,
        ))
        self.enterContext(patch(
            "elbow_helper.features.clan_reporting.wars.CLANS", self.clans,
        ))
        self.enterContext(patch(
            "elbow_helper.features.clan_reporting.wars.asyncio.sleep", new_callable=AsyncMock,
        ))
        self.clock = self.enterContext(patch(
            "elbow_helper.features.clan_reporting.wars.datetime", wraps=datetime,
        ))
        self.clock.now.return_value = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)
        self.chart = self.enterContext(patch(
            "elbow_helper.features.clan_reporting.wars.build_war_summary_chart",
            return_value=object(),
        ))
        self.threads = {
            code: SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=100 + index)))
            for index, code in enumerate(self.clans)
        }
        self.available_threads = dict(self.threads)
        self.manager = self.new_manager()

    def save_state(self, _, state, **kwargs):
        self.persisted_state = json.dumps(state)
        self.saved_states.append(json.loads(self.persisted_state))

    def new_manager(self):
        manager = _SummaryManager()
        manager.state = load_state()
        manager.bot = SimpleNamespace(wait_until_ready=AsyncMock())
        manager._get_thread = AsyncMock(side_effect=self.available_threads.get)
        manager._fetch_war_log = AsyncMock(return_value=[])
        manager._fetch_message = AsyncMock(return_value=SimpleNamespace(delete=AsyncMock()))
        return manager

    async def run_loop(self):
        await ClanReportingWarMixin._monthly_summary_loop.coro(self.manager)

    def assert_completed(self, *clans):
        self.assertEqual(
            self.manager.state["last_summary_month_by_clan"],
            {code: "2026-09" for code in clans},
        )

    async def test_missing_channel_retries_only_missing_clan_after_restart(self):
        self.available_threads["BE4"] = None

        await self.run_loop()

        self.assertIsNone(self.manager.state["last_summary_month"])
        self.assert_completed("BEH", "BES")
        self.threads["BE4"].send.assert_not_awaited()
        self.available_threads["BE4"] = self.threads["BE4"]
        self.manager = self.new_manager()

        await self.run_loop()

        self.manager._get_thread.assert_awaited_once_with("BE4")
        self.assert_completed(*self.clans)
        self.assertEqual(self.manager.state["last_summary_month"], "2026-09")
        for thread in self.threads.values():
            thread.send.assert_awaited_once()
        saved_count = len(self.saved_states)
        await self.run_loop()
        self.assertEqual(len(self.saved_states), saved_count)
        self.manager._get_thread.assert_awaited_once()

    async def test_unavailable_war_log_notice_remains_pending_until_recovery(self):
        self.manager._fetch_war_log.side_effect = lambda tag, **_: (
            None if tag == CLAN_TAGS["BE4"] else []
        )

        await self.run_loop()

        self.assertIsNone(self.manager.state["last_summary_month"])
        self.assert_completed("BEH", "BES")
        notice = self.threads["BE4"].send.call_args.kwargs["embed"]
        self.assertEqual(notice.title, "War Summary - September 2026")
        self.assertEqual(notice.description, "The war log isn't available.")
        notice_id = self.manager.state["war_summaries"]["BE4"]
        self.manager = self.new_manager()

        await self.run_loop()

        self.manager._get_thread.assert_awaited_once_with("BE4")
        self.manager._fetch_message.assert_awaited_once_with(self.threads["BE4"], notice_id)
        self.assert_completed(*self.clans)
        self.assertEqual(self.manager.state["last_summary_month"], "2026-09")
        summary = self.threads["BE4"].send.call_args.kwargs["embed"]
        self.assertEqual(summary.description, "No wars recorded for this month.")
        self.assertEqual(self.threads["BE4"].send.await_count, 2)
        self.threads["BEH"].send.assert_awaited_once()
        self.threads["BES"].send.assert_awaited_once()

    async def test_chart_failure_remains_pending_until_chart_can_be_sent(self):
        war_log = [{"endTime": "20260915T120000.000Z", "result": "win"}]
        self.manager._fetch_war_log.return_value = war_log
        self.chart.side_effect = [object(), None, object()]

        with self.assertLogs("elbow_helper.features.clan_reporting.wars", level="WARNING"):
            await self.run_loop()

        self.assertIsNone(self.manager.state["last_summary_month"])
        self.assert_completed("BEH", "BES")
        self.threads["BE4"].send.assert_not_awaited()
        self.manager = self.new_manager()
        self.manager._fetch_war_log.return_value = war_log
        self.chart.side_effect = None

        await self.run_loop()

        self.manager._get_thread.assert_awaited_once_with("BE4")
        self.threads["BE4"].send.assert_awaited_once_with(file=self.chart.return_value)
        self.assert_completed(*self.clans)

    async def test_discord_send_failure_does_not_block_other_clans_or_completion_retry(self):
        self.threads["BEH"].send.side_effect = discord.HTTPException(
            SimpleNamespace(status=503, reason="synthetic"), "synthetic",
        )

        with self.assertLogs("elbow_helper.features.clan_reporting.wars", level="WARNING"):
            await self.run_loop()

        self.assert_completed("BE4", "BES")
        self.assertNotIn("BEH", self.manager.state["war_summaries"])
        self.assertIsNone(self.manager.state["last_summary_month"])
        self.threads["BEH"].send.side_effect = None
        self.manager = self.new_manager()

        await self.run_loop()

        self.manager._get_thread.assert_awaited_once_with("BEH")
        self.assert_completed(*self.clans)
        self.threads["BE4"].send.assert_awaited_once()
        self.threads["BES"].send.assert_awaited_once()

    async def test_cancellation_preserves_already_completed_clans(self):
        self.threads["BE4"].send.side_effect = asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await self.run_loop()

        self.assertEqual(
            json.loads(self.persisted_state)["last_summary_month_by_clan"],
            {"BEH": "2026-09"},
        )
        self.threads["BE4"].send.side_effect = None
        self.manager = self.new_manager()

        await self.run_loop()

        self.assertEqual(self.manager._get_thread.await_args_list, [call("BE4"), call("BES")])
        self.threads["BEH"].send.assert_awaited_once()
        self.assert_completed(*self.clans)

    async def test_missing_clan_tag_is_retried(self):
        with patch.dict("elbow_helper.features.clan_reporting.wars.CLAN_TAGS", {"BE4": ""}):
            await self.run_loop()

        self.assert_completed("BEH", "BES")
        self.threads["BE4"].send.assert_not_awaited()
        await self.run_loop()
        self.assert_completed(*self.clans)
        self.threads["BE4"].send.assert_awaited_once()

    async def test_legacy_completed_month_is_readable_without_reposting(self):
        self.persisted_state = json.dumps({
            "last_summary_month": "2026-09", "war_summaries": {"BEH": 42},
        })
        self.manager = self.new_manager()

        await self.run_loop()

        self.assert_completed(*self.clans)
        self.assertEqual(self.manager.state["war_summaries"], {"BEH": 42})
        self.manager._get_thread.assert_not_awaited()

    async def test_per_clan_progress_takes_precedence_over_legacy_month(self):
        self.persisted_state = json.dumps({
            "last_summary_month": "2026-09",
            "last_summary_month_by_clan": {"BEH": "2026-09"},
            "war_summaries": {"BEH": 42},
        })
        self.manager = self.new_manager()

        await self.run_loop()

        self.assertEqual(self.manager._get_thread.await_args_list, [call("BE4"), call("BES")])
        self.assert_completed(*self.clans)
        self.assertEqual(self.manager.state["war_summaries"]["BEH"], 42)

    async def test_previous_completion_does_not_skip_a_new_month(self):
        self.persisted_state = json.dumps({"last_summary_month": "2026-08"})
        self.manager = self.new_manager()

        await self.run_loop()

        self.assert_completed(*self.clans)
        self.clock.now.return_value = datetime(2026, 11, 1, 8, tzinfo=timezone.utc)
        await self.run_loop()
        self.assertEqual(self.manager.state["last_summary_month"], "2026-10")
        self.assertEqual(
            self.manager.state["last_summary_month_by_clan"],
            {code: "2026-10" for code in self.clans},
        )
        for thread in self.threads.values():
            self.assertEqual(thread.send.await_count, 2)

    async def test_summaries_wait_for_scheduled_time(self):
        self.clock.now.return_value = datetime(2026, 10, 1, 7, 59, tzinfo=timezone.utc)

        await self.run_loop()

        self.manager._get_thread.assert_not_awaited()
        self.assertEqual(self.saved_states, [])


if __name__ == "__main__":
    unittest.main()
