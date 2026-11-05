from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
import sqlite3
from unittest.mock import AsyncMock, call, patch

import discord

from elbow_helper.configuration.clans import CLAN_TAGS
from elbow_helper.data.reads import WarReads
from elbow_helper.data.store.war_projection import HOUR, api_time
from elbow_helper.features.clan_reporting.helpers import ClanReportingHelperMixin
from elbow_helper.features.clan_reporting.state import load_state
from elbow_helper.features.clan_reporting.wars import ClanReportingWarMixin

from tests.data.test_war_reads import league_week, log_entry, page
from tests.data.test_wars import WarFixture, war


class _SummaryManager(ClanReportingWarMixin, ClanReportingHelperMixin):
    pass


class _LogCounts:
    """Stored log entries per clan tag, read after the month unless set otherwise."""

    def __init__(self):
        self.status, self.counts = {}, {}
        self.log_entries = AsyncMock(side_effect=self.read)

    async def read(self, tag, *, as_of=None, offset=0):
        wins, losses, ties = self.counts.get(tag, (0, 0, 0))
        rows = [{"war_id": index, "end_at": api_time("20260915T000000.000Z"), "result": result, "kind": "war"}
                for index, result in enumerate(["win"] * wins + ["lose"] * losses + ["tie"] * ties)]
        return {"status": self.status.get(tag, "available"), "rows": rows[offset:], "next_offset": None,
                "coverage": {"requested": {"as_of": as_of or READ_TIME}, "complete": True,
                             "signals": [{"last_good_at": api_time("20270101T000000.000Z"),
                                          "log_limits": {"backfill_pending": False}}]}}


class SummaryHarness:
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
        self.logs = _LogCounts()
        self.manager = self.new_manager()

    def save_state(self, _, state, **kwargs):
        self.persisted_state = json.dumps(state)
        self.saved_states.append(json.loads(self.persisted_state))

    def new_manager(self):
        manager = _SummaryManager()
        manager.state = load_state()
        manager.bot = SimpleNamespace(wait_until_ready=AsyncMock())
        manager.war_reads = self.logs
        manager._get_thread = AsyncMock(side_effect=self.available_threads.get)
        manager._fetch_message = AsyncMock(return_value=SimpleNamespace(delete=AsyncMock()))
        return manager

    async def run_loop(self):
        await ClanReportingWarMixin._monthly_summary_loop.coro(self.manager)

    def assert_completed(self, *clans):
        self.assertEqual(
            self.manager.state["last_summary_month_by_clan"],
            {code: "2026-09" for code in clans},
        )


class MonthlyWarSummaryTests(SummaryHarness, unittest.IsolatedAsyncioTestCase):
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
        self.logs.status[CLAN_TAGS["BE4"]] = "unavailable"

        await self.run_loop()

        self.assertIsNone(self.manager.state["last_summary_month"])
        self.assert_completed("BEH", "BES")
        notice = self.threads["BE4"].send.call_args.kwargs["embed"]
        self.assertEqual(notice.title, "War Summary - September 2026")
        self.assertEqual(notice.description, "The war log isn't available.")
        notice_id = self.manager.state["war_summaries"]["BE4"]
        self.logs.status.clear()
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
        self.logs.counts = {CLAN_TAGS[code]: (1, 0, 0) for code in self.clans}
        self.chart.side_effect = [object(), None, object()]

        with self.assertLogs("elbow_helper.features.clan_reporting.wars", level="WARNING"):
            await self.run_loop()

        self.assertIsNone(self.manager.state["last_summary_month"])
        self.assert_completed("BEH", "BES")
        self.threads["BE4"].send.assert_not_awaited()
        self.manager = self.new_manager()
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


SEPTEMBER = (datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 10, 1, tzinfo=timezone.utc))
SUMMARY_TIME = api_time("20261001T080000.000Z")
READ_TIME = api_time("20261001T003000.000Z")
CHART_ARGS = ("First clan", "September 2026")


def september_log():
    """Entries at every month edge and one league week, newest first as the log lists them."""
    return page(
        log_entry("#GR", "20261001T000000.000Z", "win"),
        log_entry("#CU", "20260930T235959.000Z", "tie"),
        league_week("20260920T080000.000Z"),
        log_entry("#0P", "20260915T120000.000Z", "win"),
        log_entry("#2Y", "20260901T000000.000Z", "lose"),
        log_entry("#8L", "20260831T235959.000Z", "win"),
    )


class RecordedMonthlyWarSummaryTests(SummaryHarness, WarFixture, unittest.IsolatedAsyncioTestCase):
    """Monthly summaries from the war log the collectors keep in the family record."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # "#PY" is tracked and has a log, "#LQ" is tracked and private, "#GR" is not tracked.
        for patcher in (
            patch("elbow_helper.features.clan_reporting.wars.CLAN_TAGS", {"BEH": "#PY", "BE4": "#LQ", "BES": "#GR"}),
            patch("elbow_helper.features.clan_reporting.wars.CLAN_NAMES", {"BEH": "First clan"}),
            patch("elbow_helper.data.reads.core.now_us", side_effect=lambda: SUMMARY_TIME),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.reads = WarReads(self.store)
        self.manager = self.new_manager()

    def new_manager(self):
        manager = super().new_manager()
        manager.war_reads = getattr(self, "reads", None)
        return manager

    async def record_log(self, items, *, scope="#PY", status=200, at=READ_TIME):
        await self.record(items, kind="war_log", scope=scope, status=status, at=at)

    def description(self, clan):
        return self.threads[clan].send.call_args.kwargs["embed"].description

    async def test_counts_the_month_of_the_log_with_its_edges_and_one_league_week(self):
        await self.record_log(september_log())
        # A league round is never a log entry; the week counts once through its summary.
        await self.record(war("warEnded", clans=("#PY", "#CU"), end="20260920T080000.000Z"), kind="cwl_war",
                          scope="#R0", at=READ_TIME)

        await self.run_loop()

        # In September: the tie at 23:59:59, the league week, the mid-month win and the loss at 00:00.
        self.chart.assert_called_once_with(*CHART_ARGS, 2, 1, 1)
        self.threads["BEH"].send.assert_awaited_once_with(file=self.chart.return_value)

    async def test_unavailable_private_and_empty_months_stay_apart(self):
        await self.record_log(page(log_entry("#GR", "20260820T180000.000Z", "win")))
        await self.record_log({"reason": "accessDenied"}, scope="#LQ", status=403)

        await self.run_loop()

        self.chart.assert_not_called()
        self.assertEqual(self.description("BEH"), "No wars recorded for this month.")
        self.assertEqual(self.description("BE4"), "The war log isn't available.")
        self.assertEqual(self.description("BES"), "The war log isn't available.")
        self.assert_completed("BEH")
        self.assertIsNone(self.manager.state["last_summary_month"])

    async def test_private_log_recovers_on_a_later_run_and_replaces_the_notice(self):
        await self.record_log(page())
        await self.record_log({"reason": "accessDenied"}, scope="#LQ", status=403)
        await self.run_loop()
        notice_id = self.manager.state["war_summaries"]["BE4"]
        self.manager = self.new_manager()
        entry = log_entry("#GR", "20260920T180000.000Z", "win")
        entry["clan"].update(tag="#LQ", name="Clan #LQ")
        await self.record_log(page(entry), scope="#LQ", at=READ_TIME + HOUR)

        await self.run_loop()

        self.manager._fetch_message.assert_any_await(self.threads["BE4"], notice_id)
        self.chart.assert_called_once_with("BE4", "September 2026", 1, 0, 0)
        self.assertEqual(self.manager.state["last_summary_month_by_clan"]["BE4"], "2026-09")

    async def test_log_read_before_the_month_ended_waits_for_a_later_read(self):
        await self.record_log(september_log(), at=api_time("20260930T200000.000Z"))

        await self.run_loop()

        self.threads["BEH"].send.assert_not_awaited()
        self.assertNotIn("BEH", self.manager.state.get("last_summary_month_by_clan", {}))
        self.assertNotIn("BEH", self.manager.state.get("war_summaries", {}))
        await self.record_log(september_log(), at=READ_TIME)

        await self.run_loop()

        self.chart.assert_called_once_with(*CHART_ARGS, 2, 1, 1)
        self.assertEqual(self.manager.state["last_summary_month_by_clan"]["BEH"], "2026-09")

    async def test_a_failed_record_read_retries_only_that_clan(self):
        await self.record_log(september_log())
        await self.record_log(page(), scope="#LQ")
        real = self.reads.log_entries

        async def broken_for_the_second_clan(tag, **kwargs):
            if tag == "#LQ":
                raise sqlite3.OperationalError("synthetic")
            return await real(tag, **kwargs)
        self.reads.log_entries = broken_for_the_second_clan

        with self.assertLogs("elbow_helper.features.clan_reporting.wars", level="ERROR"):
            await self.run_loop()

        self.assert_completed("BEH")
        self.threads["BE4"].send.assert_not_awaited()
        self.assertNotIn("BE4", self.manager.state["war_summaries"])
        self.reads.log_entries = real
        self.manager = self.new_manager()

        await self.run_loop()

        self.assertEqual(self.manager._get_thread.await_args_list, [call("BE4"), call("BES")])
        self.assertEqual(self.description("BE4"), "No wars recorded for this month.")
        self.threads["BEH"].send.assert_awaited_once()

    async def test_without_the_record_reads_nothing_is_posted_and_every_clan_waits(self):
        await self.record_log(september_log())
        self.manager.war_reads = None

        with self.assertLogs("elbow_helper.features.clan_reporting.wars", level="WARNING"):
            self.assertEqual(await self.manager._month_war_counts("#PY", *SEPTEMBER), ("waiting", (0, 0, 0)))
            await self.run_loop()

        for thread in self.threads.values():
            thread.send.assert_not_awaited()
        self.assertEqual(self.manager.state.get("last_summary_month_by_clan", {}), {})


if __name__ == "__main__":
    unittest.main()
