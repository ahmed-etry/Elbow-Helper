from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Sequence

import discord
from elbow_helper.discord.views import BaseTimeoutView

from .constants import LOGGER

if TYPE_CHECKING:
    from .formatting import PlanningEmbeds


class PlanningView(BaseTimeoutView):
    """Direct navigation between the attack plan's three categories."""

    def __init__(
        self,
        planning_embeds: "PlanningEmbeds",
        *,
        button_emoji_tokens: Sequence[str | None] = (),
    ):
        super().__init__(timeout=86400)
        self.planning_embeds = planning_embeds
        self.index = 0
        for button, token in zip(
            (self.overview_button, self.hero_kit_button, self.army_kit_button),
            button_emoji_tokens,
        ):
            if token:
                button.emoji = discord.PartialEmoji.from_str(token)
        self._update_controls()

    def _current_embed(self) -> discord.Embed:
        return self.planning_embeds.embed_for_page(self.index)

    def _update_controls(self) -> None:
        for index, button in enumerate(
            (self.overview_button, self.hero_kit_button, self.army_kit_button)
        ):
            button.style = (
                discord.ButtonStyle.primary
                if index == self.index
                else discord.ButtonStyle.secondary
            )

    async def _show(self, interaction: discord.Interaction) -> None:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()

            self._update_controls()
            current_embed = self._current_embed()
            if interaction.message:
                await interaction.message.edit(embed=current_embed, view=self)
            else:
                await interaction.edit_original_response(embed=current_embed, view=self)
        except discord.NotFound as exc:
            if getattr(exc, "code", None) == 10062 and interaction.message:
                try:
                    await interaction.message.edit(embed=current_embed, view=self)
                except (discord.NotFound, discord.HTTPException):
                    LOGGER.debug("Planning navigation update skipped; message no longer available.")
                return
            raise

    async def _select_page(self, interaction: discord.Interaction, index: int) -> None:
        self.index = index
        await self._show(interaction)

    @discord.ui.button(label="Overview", style=discord.ButtonStyle.primary)
    async def overview_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._select_page(interaction, 0)

    @discord.ui.button(label="Hero Kit", style=discord.ButtonStyle.secondary)
    async def hero_kit_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._select_page(interaction, 1)

    @discord.ui.button(label="Army Kit", style=discord.ButtonStyle.secondary)
    async def army_kit_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._select_page(interaction, 2)
