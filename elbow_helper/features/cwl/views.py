"""Interactive CWL views."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import discord
from elbow_helper.discord.views import BaseTimeoutView
from .config import MANUAL_DASHBOARD_REFRESH_COOLDOWN_SECONDS

if TYPE_CHECKING:
    from .cog import CwlManagement


class CwlTransferHubView(BaseTimeoutView):
    def __init__(self, cog: CwlManagement, *, placements_released: bool):
        super().__init__(timeout=None)
        self.cog = cog

        accounts_button = discord.ui.Button(
            label="Where Am I Playing?",
            style=discord.ButtonStyle.success,
            custom_id="cwl:transfer_hub:accounts",
            disabled=not placements_released,
        )
        accounts_button.callback = self.show_accounts
        self.add_item(accounts_button)

        channels_button = discord.ui.Button(
            label="CWL Channels",
            style=discord.ButtonStyle.secondary,
            custom_id="cwl:transfer_hub:channels",
            disabled=not placements_released,
        )
        channels_button.callback = self.show_channels
        self.add_item(channels_button)

        self.add_item(
            discord.ui.Button(
                label="See All Rosters",
                style=discord.ButtonStyle.link,
                url=cog._full_rosters_url(),
            )
        )

    async def show_accounts(self, interaction: discord.Interaction) -> None:
        await self.cog.show_member_cwl(interaction)

    async def show_channels(self, interaction: discord.Interaction) -> None:
        await self.cog.show_member_cwl_channels(interaction)


class CwlPrepRefreshView(BaseTimeoutView):
    def __init__(self, cog: CwlManagement, clan_code: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.clan_code = clan_code
        # Custom ID must stay stable across restarts for persistent views.
        refresh_button = discord.ui.Button(
            emoji="\U0001F501",
            style=discord.ButtonStyle.secondary,
            custom_id=f"cwl:prep_refresh:{clan_code.lower()}",
        )
        refresh_button.callback = self.refresh
        self.add_item(refresh_button)

    async def refresh(self, interaction: discord.Interaction) -> None:
        lock = self.cog._get_manual_dashboard_refresh_lock(self.clan_code)
        if lock.locked():
            await interaction.response.send_message(
                "This clan's roster is already being updated.",
                ephemeral=True,
            )
            return

        now_ts = time.time()
        last_ts = self.cog._manual_dashboard_refresh_last_ts.get(self.clan_code, 0.0)
        cooldown_seconds = MANUAL_DASHBOARD_REFRESH_COOLDOWN_SECONDS
        remaining = int(cooldown_seconds - (now_ts - last_ts))
        if remaining > 0:
            if remaining == 1:
                cooldown_message = "Wait 1 second before updating the rosters again."
            else:
                cooldown_message = (
                    f"Wait {remaining} seconds before updating the rosters again."
                )
            await interaction.response.send_message(
                cooldown_message,
                ephemeral=True,
            )
            return

        try:
            await interaction.response.defer()
        except discord.NotFound:
            return
        async with lock:
            ok = await self.cog._refresh_dashboard_with_retry(self.clan_code, context="manual")
            if not ok:
                await interaction.followup.send("The roster couldn't be updated. Try again in a moment.", ephemeral=True)
                return
            self.cog._manual_dashboard_refresh_last_ts[self.clan_code] = time.time()
