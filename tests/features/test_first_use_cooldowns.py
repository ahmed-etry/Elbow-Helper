from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import discord

from elbow_helper.features.clan_reporting.elders import ClanReportingElderMixin
from elbow_helper.features.examination.cog import Examination
from elbow_helper.features.recruitment.cog import Recruitment
from elbow_helper.features.rosters.cog import Rosters


class FirstUseCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_elder_board_can_repost_during_low_system_uptime(self) -> None:
        manager = object.__new__(ClanReportingElderMixin)
        manager._board_last_repost_at = {}

        async def run_operation(clan_code, operation, coro_factory):
            return await coro_factory()

        manager._run_discord_http_operation = AsyncMock(side_effect=run_operation)
        channel = MagicMock(spec=discord.TextChannel)

        async def history(**kwargs):
            for message_id in (903, 902, 901):
                yield SimpleNamespace(id=message_id)

        channel.history = history
        trigger = SimpleNamespace(id=903)

        with patch(
            "elbow_helper.features.clan_reporting.elders.time.monotonic",
            return_value=60.0,
        ):
            should_repost = await manager._should_repost_missing_elder_message(
                channel,
                900,
                "BEH",
                trigger_message=trigger,
            )

        self.assertTrue(should_repost)

    async def test_missing_elder_board_still_honors_a_real_cooldown(self) -> None:
        manager = object.__new__(ClanReportingElderMixin)
        manager._board_last_repost_at = {"BEH": 50.0}
        manager._run_discord_http_operation = AsyncMock()
        channel = MagicMock(spec=discord.TextChannel)
        channel.history = MagicMock()

        with patch(
            "elbow_helper.features.clan_reporting.elders.time.monotonic",
            return_value=60.0,
        ):
            should_repost = await manager._should_repost_missing_elder_message(
                channel,
                900,
                "BEH",
                trigger_message=SimpleNamespace(id=903),
            )

        self.assertFalse(should_repost)
        manager._run_discord_http_operation.assert_not_awaited()

    async def test_roster_can_refresh_during_low_system_uptime(self) -> None:
        cog = object.__new__(Rosters)
        cog._refresh_times = {}
        cog.service = SimpleNamespace(get=AsyncMock(return_value=None))
        interaction = MagicMock(spec=discord.Interaction)

        with (
            patch(
                "elbow_helper.features.rosters.cog.time.monotonic",
                return_value=10.0,
            ),
            patch(
                "elbow_helper.features.rosters.cog.warn",
                new=AsyncMock(),
            ) as mocked_warn,
        ):
            await cog.handle_refresh(interaction, 7)

        cog.service.get.assert_awaited_once_with(7)
        mocked_warn.assert_awaited_once_with(
            interaction,
            "That roster no longer exists.",
        )

    async def test_roster_still_honors_a_real_refresh_cooldown(self) -> None:
        cog = object.__new__(Rosters)
        cog._refresh_times = {7: 5.0}
        cog.service = SimpleNamespace(get=AsyncMock())
        interaction = MagicMock(spec=discord.Interaction)

        with (
            patch(
                "elbow_helper.features.rosters.cog.time.monotonic",
                return_value=10.0,
            ),
            patch(
                "elbow_helper.features.rosters.cog.warn",
                new=AsyncMock(),
            ) as mocked_warn,
        ):
            await cog.handle_refresh(interaction, 7)

        cog.service.get.assert_not_awaited()
        mocked_warn.assert_awaited_once_with(
            interaction,
            "This roster was just refreshed. Try again in a moment.",
        )

    def test_recruitment_first_recurring_issue_is_a_warning(self) -> None:
        cog = object.__new__(Recruitment)
        cog._recurring_issue_log_times = {}
        cog.logger = MagicMock()

        with patch(
            "elbow_helper.features.recruitment.cog.time.monotonic",
            return_value=10.0,
        ):
            cog._warn_recurring_issue("test", "Problem: %s", "details")
            cog._warn_recurring_issue("test", "Problem: %s", "details")

        cog.logger.warning.assert_called_once_with("Problem: %s", "details")
        cog.logger.debug.assert_called_once_with("Problem: %s", "details")

    def test_examination_first_reorder_issue_is_a_warning(self) -> None:
        cog = object.__new__(Examination)
        cog._ticket_reorder_issue_log_times = {}
        cog.logger = MagicMock()

        with patch(
            "elbow_helper.features.examination.cog.time.monotonic",
            return_value=10.0,
        ):
            cog._warn_ticket_reorder_issue("test", "Problem: %s", "details")
            cog._warn_ticket_reorder_issue("test", "Problem: %s", "details")

        cog.logger.warning.assert_called_once_with("Problem: %s", "details")
        cog.logger.debug.assert_called_once_with("Problem: %s", "details")


if __name__ == "__main__":
    unittest.main()
