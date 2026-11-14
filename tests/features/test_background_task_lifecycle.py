from __future__ import annotations

import asyncio
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from discord.ext import tasks

from elbow_helper.core.background import start_resilient_loop
from elbow_helper.features.account_links.cog import AccountLinks
from elbow_helper.features.clan_reporting.cog import ClanReporting
from elbow_helper.features.examination.cog import Examination
from elbow_helper.features.event_stats.cog import EventStatsCog
from elbow_helper.features.member_lifecycle.cog import MemberLifecycle
from elbow_helper.features.recruitment.cog import Recruitment
from elbow_helper.features.wars.cog import WarManager


def _loop(*, running: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        is_running=MagicMock(return_value=running),
        cancel=MagicMock(),
    )


async def _blocked(started: asyncio.Event) -> None:
    started.set()
    await asyncio.Event().wait()


class BackgroundTaskLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_feature_loops_continue_after_unlisted_errors(self) -> None:
        real_sleep = asyncio.sleep

        async def yield_sleep(*args, **kwargs):
            await real_sleep(0)

        for feature in ("examination", "events", "wars"):
            with self.subTest(feature=feature):
                reached = asyncio.Event()
                attempts = 0

                def attempt(*args, **kwargs):
                    nonlocal attempts
                    attempts += 1
                    if attempts == 1:
                        raise LookupError("background pass failed")
                    reached.set()
                    return {}

                bot = MagicMock()
                bot.wait_until_ready = AsyncMock()
                bot.boot_complete = None
                with (
                    patch("asyncio.sleep", side_effect=yield_sleep),
                    patch("discord.ext.tasks.ExponentialBackoff.delay", return_value=0),
                    patch("discord.ext.tasks._log.exception") as logged,
                ):
                    if feature == "examination":
                        cog = object.__new__(Examination)
                        cog.bot = bot
                        cog._followup_task = None
                        cog._availability_view_registered = True
                        cog._promo_intake_view_registered = True
                        cog._reconcile_promo_route_controls = AsyncMock()
                        cog._post_panel = AsyncMock()
                        cog._get_cases = MagicMock(side_effect=attempt)
                        await cog.on_ready()
                        task = cog._followup_task
                    elif feature == "events":
                        cog = EventStatsCog(bot, initial_state={"events": []})
                        cog._get_guild = MagicMock(return_value=object())
                        cog.force_refresh = AsyncMock(side_effect=attempt)
                        task = cog._startup_task
                    else:
                        cog = object.__new__(WarManager)
                        cog.bot = bot
                        cog._startup_sync_task = asyncio.create_task(real_sleep(0))
                        cog._startup_sync_done = asyncio.Event()
                        cog._startup_sync_done.set()
                        cog._summary_cleanup_task = None
                        cog._cleanup_summary_messages = AsyncMock(side_effect=attempt)
                        await cog.on_ready()
                        task = cog._summary_cleanup_task
                    completed = asyncio.create_task(reached.wait())
                    try:
                        await asyncio.wait({task, completed}, timeout=1,
                                           return_when=asyncio.FIRST_COMPLETED)
                        self.assertTrue(reached.is_set(), "loop ended after one unexpected error")
                        logged.assert_called()
                    finally:
                        task.cancel()
                        completed.cancel()
                        await asyncio.gather(task, completed, return_exceptions=True)

    async def test_recurring_loop_retries_an_unexpected_failure(self) -> None:
        attempts = 0
        completed = asyncio.Event()

        @tasks.loop(seconds=0.01)
        async def recurring_work() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("unexpected failure")
            completed.set()

        with (
            patch("discord.ext.tasks.ExponentialBackoff.delay", return_value=0),
            self.assertLogs("discord.ext.tasks", level="ERROR"),
        ):
            task = start_resilient_loop(recurring_work)
            await asyncio.wait_for(completed.wait(), timeout=1)
            recurring_work.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertGreaterEqual(attempts, 2)

    async def test_account_ready_refresh_is_deduplicated_and_cancelled(self) -> None:
        cog = object.__new__(AccountLinks)
        cog._snapshot_ready = asyncio.Event()
        cog._ready_refresh_task = None
        cog._poll_clans_loop = _loop()
        started = asyncio.Event()

        async def refresh_now() -> None:
            await _blocked(started)

        cog.refresh_now = AsyncMock(side_effect=refresh_now)

        await cog.on_ready()
        await started.wait()
        task = cog._ready_refresh_task
        await cog.on_ready()

        self.assertIs(cog._ready_refresh_task, task)
        cog.refresh_now.assert_called_once()

        cog.cog_unload()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        cog._poll_clans_loop.cancel.assert_called_once()

    async def test_recruitment_ticket_processing_is_cancelled_on_unload(self) -> None:
        cog = object.__new__(Recruitment)
        cog.logger = logging.getLogger("test.recruitment.tasks")
        cog._applicant_ticket_tasks = set()
        cog.recruitment_state = SimpleNamespace(events=SimpleNamespace(unregister_member_deletion=MagicMock()))
        for name in (
            "check_expired_trials",
            "cleanup_trial_reminders",
            "organize_tickets",
            "check_inactive_tickets",
            "cleanup_old_ticket_reminders",
            "cleanup_applicant_ai",
        ):
            setattr(cog, name, _loop())
        started = asyncio.Event()

        async def process_ticket(channel: object) -> None:
            await _blocked(started)

        cog._process_applicant_ticket = AsyncMock(side_effect=process_ticket)

        cog._start_applicant_ticket_processing(SimpleNamespace(id=42))
        await started.wait()
        task = next(iter(cog._applicant_ticket_tasks))

        cog.cog_unload()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())

    async def test_clan_reporting_tasks_are_cancelled_on_unload(self) -> None:
        cog = object.__new__(ClanReporting)
        cog._background_tasks = set()
        cog._refresh_task = None
        cog._monthly_summary_loop = _loop()
        started = asyncio.Event()

        task = cog._start_background_task(
            _blocked(started),
            name="test-clan-reporting-task",
        )
        await started.wait()

        cog.cog_unload()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())

    async def test_examination_tasks_are_observed_and_cancelled_on_unload(self) -> None:
        cog = object.__new__(Examination)
        cog.logger = logging.getLogger("test.examination.tasks")
        cog._background_tasks = set()
        cog._followup_task = None
        cog._scan_task = None
        cog.organize_tickets = _loop()
        cog.cleanup_deprecated_routing_messages = _loop()

        async def fail() -> None:
            raise RuntimeError("background failure")

        with self.assertLogs("test.examination.tasks", level="ERROR") as captured:
            task = cog._start_background_task(fail(), name="test-exam-task")
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)

        self.assertFalse(cog._background_tasks)
        self.assertIn("Examination background task failed", captured.output[0])

        started = asyncio.Event()
        blocked_task = cog._start_background_task(
            _blocked(started),
            name="test-exam-blocked-task",
        )
        await started.wait()
        cog.cog_unload()
        await asyncio.gather(blocked_task, return_exceptions=True)
        self.assertTrue(blocked_task.cancelled())

    async def test_report_cleanup_is_cancelled_on_unload(self) -> None:
        cog = object.__new__(MemberLifecycle)
        cog._report_cleanup_tasks = set()
        cog._ticket_index_task = None
        cog.weekly_report = _loop()
        cog.applicant_linger_scan = _loop()
        cog.lifecycle_state = SimpleNamespace(events=SimpleNamespace(unregister_member_deletion=MagicMock()))
        started = asyncio.Event()

        cog._start_report_cleanup(_blocked(started), message_id=99)
        await started.wait()
        task = next(iter(cog._report_cleanup_tasks))

        cog.cog_unload()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        cog.lifecycle_state.events.unregister_member_deletion.assert_called_once_with("lifecycle")


if __name__ == "__main__":
    unittest.main()
