"""Persistent views for clan-reporting missing-elder boards."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from elbow_helper.discord.views import BaseTimeoutView


if TYPE_CHECKING:
    from .cog import ClanReporting


class MissingElderBoardView(BaseTimeoutView):
    """Refresh controls for tracked missing-elder board messages."""

    def __init__(self, cog: "ClanReporting", clan_code: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.clan_code = clan_code

        refresh_button = discord.ui.Button(
            emoji="\U0001F501",
            style=discord.ButtonStyle.secondary,
            custom_id=f"clan_data:missing_elder_refresh:{clan_code}",
        )
        refresh_button.callback = self._on_refresh
        self.add_item(refresh_button)

    async def _on_refresh(self, interaction: discord.Interaction) -> None:
        await self.cog.refresh_missing_elder_board(interaction, self.clan_code)
