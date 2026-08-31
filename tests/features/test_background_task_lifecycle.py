from __future__ import annotations

import asyncio
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.features.account_links.cog import AccountLinks
from elbow_helper.features.clan_reporting.cog import ClanReporting
from elbow_helper.features.examination.cog import Examination
from elbow_helper.features.member_lifecycle.cog import MemberLifecycle
from elbow_helper.features.recruitment.cog import Recruitment


def _loop(*, running: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        is_running=MagicMock(return_value=running),
        cancel=MagicMock(),
    )


async def _blocked(started: asyncio.Event) -> None:
    started.set()
    await asyncio.Event().wait()


class BackgroundTaskLifecycleTests(unittest.IsolatedAsyncioTestCase):
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
        cog.state = {}
        started = asyncio.Event()

        cog._start_report_cleanup(_blocked(started), message_id=99)
        await started.wait()
        task = next(iter(cog._report_cleanup_tasks))

        with patch("elbow_helper.features.member_lifecycle.cog.save_state"):
            cog.cog_unload()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())


if __name__ == "__main__":
    unittest.main()
