"""Persistent views for clan transfer queue messages."""

from __future__ import annotations

import discord
from elbow_helper.discord.views import BaseTimeoutView


class ClanTransfersView(BaseTimeoutView):
    """Clear-queue action for one clan transfer queue."""

    def __init__(self, clan_code: str, enabled: bool = True):
        super().__init__(timeout=None)
        self.clan_code = clan_code

        clear_button = discord.ui.Button(
            label="Transfers Done — Clear Queue",
            style=discord.ButtonStyle.success,
            custom_id=f"clan_transfers:clear:{clan_code}",
        )
        clear_button.disabled = not enabled
        clear_button.callback = self.clear_queue_callback
        self.add_item(clear_button)

    async def clear_queue_callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("ClanTransfers")
        if cog is None:
            await interaction.response.send_message("Transfers are not available right now.", ephemeral=True)
            return
        await cog.handle_clear_queue(interaction, self.clan_code)
