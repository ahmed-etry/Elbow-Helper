"""Clan exports require a prepared workbook before delivery."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from discord import app_commands

from elbow_helper.features.clan_health.commands.clan import (
    ClanHealthClanCommandMixin, ClanHealthCommandResult,
)


class ClanHealthCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_export_is_reported_without_attempting_delivery(self):
        cog = ClanHealthClanCommandMixin()
        cog._has_access = lambda _: True
        cog.run_clan_health_export = AsyncMock(return_value=ClanHealthCommandResult("complete"))
        cog.send_prepared_health_export = AsyncMock()
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=101),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with self.assertLogs("elbow_helper.features.clan_health.commands.clan", level="ERROR"):
            await cog._export_clan_health(
                interaction, app_commands.Choice(name="Synthetic", value="SYNTHETIC"),
            )
        cog.send_prepared_health_export.assert_not_awaited()
        interaction.followup.send.assert_awaited_once_with(
            "Could not generate the spreadsheet right now. Try again in a moment.",
        )
