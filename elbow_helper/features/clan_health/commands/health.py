"""Top-level `/health` command group."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any, Optional

import discord
from discord import app_commands

from elbow_helper.discord.interactions import deny
from elbow_helper.discord.views import BaseErrorModal

from ..config import CLAN_CHOICES, CLAN_EXPORT_ORDER
from ..ui import ClanConfigHomeView

LOGGER = logging.getLogger(__name__)


HEALTH_PERIOD_CHOICES = [
    app_commands.Choice(name="Last 7 days", value="last_7d"),
    app_commands.Choice(name="Last 14 days", value="last_14d"),
    app_commands.Choice(name="Last 30 days", value="last_30d"),
    app_commands.Choice(name="Custom dates", value="custom"),
]


class HealthDateRangeModal(BaseErrorModal):
    def __init__(
        self,
        cog: Any,
        *,
        player: Optional[str] = None,
        clan: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        super().__init__(title="Custom Health Period")
        self.cog = cog
        self.player = player
        self.clan = clan
        self.period = app_commands.Choice(name="Custom dates", value="custom")

        self.date_from = discord.ui.TextInput(
            label="Start date",
            placeholder="YYYY-MM-DD",
            required=True,
            max_length=10,
        )
        self.date_to = discord.ui.TextInput(
            label="End date",
            placeholder="YYYY-MM-DD",
            required=True,
            max_length=10,
        )
        self.add_item(self.date_from)
        self.add_item(self.date_to)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        date_from = self.date_from.value.strip()
        date_to = self.date_to.value.strip()
        if self.player is not None:
            await self.cog._export_player_health(
                interaction,
                player=self.player,
                window=self.period,
                date_from=date_from,
                date_to=date_to,
            )
            return

        assert self.clan is not None
        await self.cog._export_clan_health(
            interaction,
            clan=self.clan,
            window=self.period,
            date_from=date_from,
            date_to=date_to,
        )


class ClanHealthRootCommandMixin:
    health = app_commands.Group(
        name="health",
        description="View player and clan health reports or update clan expectations.",
    )
    HEALTH_CONFIG_CHOICES = [app_commands.Choice(name=code, value=code) for code in CLAN_EXPORT_ORDER]

    async def health_player_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        del interaction
        try:
            rows = await asyncio.wait_for(
                asyncio.to_thread(self.repository.search_players, current, 25),
                timeout=1.5,
            )
            return [
                app_commands.Choice(
                    name=(
                        f"{row.get('player_name') or row['player_tag']} - "
                        f"{row.get('clan_code') or '-'} - "
                        f"TH{int(row.get('townhall') or 0)} - {row['player_tag']}"
                    )[:100],
                    value=str(row["player_tag"]),
                )
                for row in rows
            ]
        except TimeoutError:
            LOGGER.warning("Health player autocomplete timed out")
            return []
        except (KeyError, TypeError, ValueError, sqlite3.Error):
            LOGGER.exception("Health player autocomplete failed")
            return []

    @health.command(name="player", description="Export a health report for a Clash account.")
    @app_commands.describe(
        account="Choose a Clash account or enter a player tag.",
        period="Report period. Defaults to the last 30 days.",
    )
    @app_commands.autocomplete(account=health_player_autocomplete)
    @app_commands.choices(period=HEALTH_PERIOD_CHOICES)
    async def health_player(
        self,
        interaction: discord.Interaction,
        account: str,
        period: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        account = str(account or "").strip()
        if period is not None and period.value == "custom":
            if not self._has_access(interaction):
                await deny(interaction)
                return
            await interaction.response.send_modal(HealthDateRangeModal(self, player=account))
            return
        await self._export_player_health(interaction, player=account, window=period)

    @health.command(name="clan", description="Export a clan or family health report.")
    @app_commands.describe(
        clan="Clan to export, or ALL for the full family set.",
        period="Report period. Defaults to the last 30 days.",
    )
    @app_commands.choices(clan=CLAN_CHOICES, period=HEALTH_PERIOD_CHOICES)
    async def health_clan(
        self,
        interaction: discord.Interaction,
        clan: app_commands.Choice[str],
        period: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        if period is not None and period.value == "custom":
            if not self._has_access(interaction):
                await deny(interaction)
                return
            await interaction.response.send_modal(HealthDateRangeModal(self, clan=clan))
            return
        await self._export_clan_health(interaction, clan=clan, window=period)

    @health.command(name="settings", description="Set the activity expectations used in a clan's health reports.")
    @app_commands.describe(clan="Clan whose health report expectations you want to update.")
    @app_commands.choices(clan=HEALTH_CONFIG_CHOICES)
    async def health_settings(
        self,
        interaction: discord.Interaction,
        clan: app_commands.Choice[str],
    ) -> None:
        if not self._has_access(interaction):
            await deny(interaction)
            return
        await ClanConfigHomeView.open(interaction, clan.value)
