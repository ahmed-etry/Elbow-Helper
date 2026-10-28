from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from elbow_helper.infrastructure.discord.publishing import PublishingQueue
from elbow_helper.features.cwl.announcements import CwlAnnouncementMixin
from elbow_helper.features.cwl.bonus.dashboard import CwlBonusDashboardMixin
from elbow_helper.features.cwl.dashboard import CwlDashboardMixin


class CwlPublishingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.queue = PublishingQueue()
        self.addAsyncCleanup(self.queue.close)

    def image(self):
        image = discord.File(BytesIO(b"synthetic image"), filename="chart.png")
        self.addCleanup(image.close)
        return image

    def dashboard(self):
        owner = CwlDashboardMixin()
        owner.bot = SimpleNamespace(publishing=self.queue)
        owner._dashboard_refresh_warning_state = {}
        owner._log_dashboard_refresh_failure = MagicMock()
        owner.dashboard_state = {"prep_message_ids": {"BEH": 101}, "stars_message_ids": {"BEH": 102}}
        owner._build_prep_preview_payload = AsyncMock(return_value=(discord.Embed(title="prep"), self.image()))
        owner._build_dashboard_stars_payload = AsyncMock(return_value=(discord.Embed(title="stars"), self.image()))
        owner._find_dashboard_messages = AsyncMock(return_value=[])
        owner._save_dashboard_state = MagicMock()
        messages = {id: SimpleNamespace(id=id, edit=AsyncMock()) for id in (101, 102)}
        thread = SimpleNamespace(id=10, archived=False, edit=AsyncMock(),
                                 fetch_message=AsyncMock(side_effect=messages.get))
        owner._resolve_dashboard_thread = AsyncMock(return_value=thread)
        return owner, thread, messages

    async def test_dashboard_success_applies_both_images(self):
        owner, _, messages = self.dashboard()
        self.assertTrue(await owner._refresh_dashboard_with_retry("BEH", "manual"))
        for message in messages.values():
            message.edit.assert_awaited_once()
            self.assertEqual(message.edit.await_args.kwargs["attachments"][0].filename, "chart.png")
        owner._log_dashboard_refresh_failure.assert_not_called()

    async def test_failed_dashboard_edit_returns_failure_without_a_second_retry_layer(self):
        owner, _, messages = self.dashboard()
        messages[101].edit.side_effect = discord.HTTPException(
            SimpleNamespace(status=503, reason="synthetic"), "synthetic failure",
        )
        with patch("elbow_helper.infrastructure.discord.publishing.HTTP_RETRY_DELAYS", (0, 0)):
            with self.assertLogs("elbow_helper.discord.publishing", "WARNING") as logs:
                self.assertFalse(await owner._refresh_dashboard_with_retry("BEH", "manual"))
        self.assertEqual(messages[101].edit.await_count, 3)
        messages[102].edit.assert_not_awaited()
        owner._log_dashboard_refresh_failure.assert_not_called()
        self.assertEqual(len(logs.output), 1)

    async def test_failed_unarchive_does_not_report_a_completed_dashboard(self):
        owner, thread, messages = self.dashboard()
        thread.archived = True
        thread.edit.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="synthetic"), "synthetic failure",
        )
        with self.assertLogs("elbow_helper.discord.publishing", "WARNING"):
            self.assertFalse(await owner._refresh_dashboard_with_retry("BEH", "manual"))
        thread.edit.assert_awaited_once()
        for message in messages.values():
            message.edit.assert_not_awaited()

    async def test_bonus_board_only_records_success_after_publication(self):
        owner = CwlBonusDashboardMixin()
        owner.bot = SimpleNamespace(publishing=self.queue)
        owner._bonus_month_key = lambda: "2026-10"
        board = {"message_id": 101, "channel_id": 10}
        owner._ensure_bonus_board = MagicMock(return_value=("key", board))
        owner._build_bonus_dashboard_embed = lambda board: discord.Embed(title="bonus")
        owner._build_bonus_dashboard_file = lambda board: self.image()
        owner.bonus_dashboard_store = SimpleNamespace(save=MagicMock())
        message = SimpleNamespace(id=101, edit=AsyncMock())
        channel = SimpleNamespace(id=10, fetch_message=AsyncMock(return_value=message))
        message.edit.side_effect = discord.HTTPException(
            SimpleNamespace(status=503, reason="synthetic"), "synthetic failure",
        )
        with patch("elbow_helper.infrastructure.discord.publishing.HTTP_RETRY_DELAYS", (0, 0)):
            with self.assertLogs("elbow_helper.discord.publishing", "WARNING"):
                self.assertFalse(await owner._post_bonus_dashboard(
                    mode="final", target_channel=channel, ping_helper=True,
                ))
        self.assertNotIn("updated_at", board)
        owner.bonus_dashboard_store.save.assert_not_called()
        message.edit.side_effect = None
        self.assertTrue(await owner._post_bonus_dashboard(
            mode="final", target_channel=channel, ping_helper=True,
        ))
        owner.bonus_dashboard_store.save.assert_called_once()
        self.assertIn("updated_at", board)

    async def test_bonus_scheduler_retries_a_failed_publication_on_the_next_pass(self):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 14, 8, tzinfo=timezone.utc)

        owner = CwlAnnouncementMixin()
        channel = MagicMock(spec=discord.TextChannel)
        owner.bot = SimpleNamespace(wait_until_ready=AsyncMock(), get_channel=lambda id: channel)
        owner.roster_automation = SimpleNamespace(cwl_signup_window=AsyncMock(return_value=None))
        owner._cleanup_expired_transfer_reminders = AsyncMock()
        owner._sent_keys = {"cleanup-2026-10"}
        owner._post_bonus_dashboard = AsyncMock(side_effect=[False, True])
        owner._save_scheduler_state = MagicMock()
        with (
            patch("elbow_helper.features.cwl.announcements.datetime", Clock),
            patch("elbow_helper.features.cwl.announcements.CWL_BONUS_ECONOMY_ENABLED", True),
            patch("elbow_helper.features.cwl.announcements.BONUS_AUTOMATION_START_MONTH_KEY", 0),
        ):
            await CwlAnnouncementMixin.reminder_loop.coro(owner)
            self.assertNotIn("bonus-board-2026-10", owner._sent_keys)
            await CwlAnnouncementMixin.reminder_loop.coro(owner)
        self.assertIn("bonus-board-2026-10", owner._sent_keys)
        self.assertEqual(owner._post_bonus_dashboard.await_count, 2)
        owner._save_scheduler_state.assert_called_once()


if __name__ == "__main__":
    unittest.main()
