from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import unittest
from unittest.mock import AsyncMock
from unittest.mock import call
from unittest.mock import MagicMock
from unittest.mock import patch

from discord import app_commands

from elbow_helper.features.cwl.announcements import CwlAnnouncementMixin
from elbow_helper.features.cwl.announcements import _signup_reminder_times
from elbow_helper.features.cwl.templates import ROSTER_TEMPLATE
from elbow_helper.features.cwl.templates import SIGNUP_STATEMENT
from elbow_helper.features.rosters.services.scheduling import ScheduleWindow
from elbow_helper.configuration.channels import CWL_SIGNUP


class CwlSignupAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    def test_roster_announcement_routes_members_through_the_current_message(self) -> None:
        hub_url = "https://discord.com/channels/1/2/3"
        content = ROSTER_TEMPLATE.format(
            intro_text="Intro",
            deadline_section="Deadline",
            hub_message_url=hub_url,
            war_specialist_role="Extra Wars",
        )

        self.assertIn(
            f"Use **Where Am I Playing?** under [**CWL Rosters and Transfers**]({hub_url}).",
            content,
        )
        self.assertIn("Use **See All Rosters** there.", content)
        self.assertIn("under **Where I war** on your Discord profile", content)
        self.assertNotIn("In the thread above", content)
        self.assertNotIn("if you click on the roster name", content)
        self.assertNotIn("Use **CWL Channels**", content)

    def test_reminders_follow_the_roster_closing_time_without_colliding(self) -> None:
        opens_at = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        closes_at = datetime(2026, 7, 30, 18, 0, tzinfo=timezone.utc)

        first, final = _signup_reminder_times(opens_at, closes_at)

        self.assertEqual(first, opens_at + timedelta(days=7))
        self.assertEqual(final, closes_at - timedelta(days=2))
        self.assertEqual(
            _signup_reminder_times(opens_at, opens_at + timedelta(days=2)),
            (None, None),
        )

    async def test_posted_roster_announcement_releases_the_current_cycles(self) -> None:
        target_channel = MagicMock()
        target_channel.id = 2
        target_channel.guild.id = 1
        sent_message = MagicMock()
        interaction = MagicMock()
        interaction.user = MagicMock()
        interaction.client.get_channel.return_value = target_channel
        interaction.response.send_message = AsyncMock()
        interaction.followup.send = AsyncMock()

        cog = object.__new__(CwlAnnouncementMixin)
        cog._has_any_role = MagicMock(return_value=True)
        cog.ensure_transfer_hub = AsyncMock(side_effect=[True, True])
        cog._transfer_hub_url = MagicMock(
            return_value="https://discord.com/channels/1/2/3"
        )
        cycles = {str(roster_id): roster_id + 20 for roster_id in range(4, 12)}
        cog._current_cwl_roster_cycles = AsyncMock(return_value=cycles)
        cog._resolve_next_local_deadline = MagicMock(
            return_value=datetime(2026, 8, 1, 20, 0, tzinfo=timezone.utc)
        )
        cog._send_chunked = AsyncMock(return_value=[sent_message])
        cog._react_with_detected_emojis = AsyncMock()
        cog._release_cwl_placements = MagicMock()

        with patch(
            "elbow_helper.features.cwl.announcements.resolve_timezone",
            return_value=timezone.utc,
        ):
            await CwlAnnouncementMixin.roster_announcement(
                cog,
                interaction,
                app_commands.Choice(name="Single deadline for all", value="single"),
                "01-20:00",
                "UTC",
            )

        cog._release_cwl_placements.assert_called_once_with(cycles)
        self.assertEqual(cog.ensure_transfer_hub.await_count, 2)
        interaction.followup.send.assert_awaited_with(
            "The roster announcement is live.",
            ephemeral=True,
        )

    async def test_opening_announcement_uses_the_signup_channel_once(self) -> None:
        now = datetime(2026, 7, 18, 10, 5, tzinfo=timezone.utc)
        window = ScheduleWindow(
            cycle_key="2026-07",
            opens_at=datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc),
            closes_at=datetime(2026, 7, 30, 18, 0, tzinfo=timezone.utc),
        )
        roster = MagicMock()
        roster_automation = MagicMock()
        roster_automation.cwl_signup_window = AsyncMock(
            return_value=(roster, window)
        )
        roster_automation.claim_event = AsyncMock(side_effect=[True, False])
        roster_automation.release_event = AsyncMock()
        channel = MagicMock()
        signup_message = MagicMock()
        channel.send = AsyncMock(return_value=signup_message)
        bot = MagicMock()
        bot.wait_until_ready = AsyncMock()
        bot.get_channel.side_effect = (
            lambda channel_id: channel if channel_id == CWL_SIGNUP else None
        )

        cog = object.__new__(CwlAnnouncementMixin)
        cog.bot = bot
        cog.roster_automation = roster_automation
        cog._sent_keys = {"cleanup-2026-7"}
        cog._cleanup_expired_transfer_reminders = AsyncMock()
        cog._react_with_detected_emojis = AsyncMock()
        cog._save_scheduler_state = MagicMock()

        with patch("elbow_helper.features.cwl.announcements.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = now
            mocked_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            await CwlAnnouncementMixin.reminder_loop.coro(cog)

        channel.send.assert_awaited_once_with(SIGNUP_STATEMENT)
        cog._react_with_detected_emojis.assert_awaited_once_with(
            signup_message,
            SIGNUP_STATEMENT,
        )
        self.assertIn("signup-2026-07", cog._sent_keys)

        cog._sent_keys = {"cleanup-2026-7"}
        with patch("elbow_helper.features.cwl.announcements.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = now
            mocked_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            await CwlAnnouncementMixin.reminder_loop.coro(cog)

        channel.send.assert_awaited_once()
        roster_automation.claim_event.assert_has_awaits(
            [
                call(roster.id, "2026-07", "opening"),
                call(roster.id, "2026-07", "opening"),
            ]
        )
