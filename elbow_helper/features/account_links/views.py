"""Interactive views for recruiter review of suggested links."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from elbow_helper.discord.views import BaseTimeoutView


if TYPE_CHECKING:
    from .cog import AccountLinks


class SuggestionReviewView(BaseTimeoutView):
    def __init__(self, cog: "AccountLinks", player_tag: str, *, has_candidate: bool):
        super().__init__(timeout=None)
        self.cog = cog
        self.player_tag = player_tag

        if has_candidate:
            confirm_button = discord.ui.Button(
                label="Confirm",
                style=discord.ButtonStyle.success,
                custom_id=f"clan_links:confirm:{player_tag}",
            )
            confirm_button.callback = self._confirm
            self.add_item(confirm_button)

            not_them_button = discord.ui.Button(
                label="Wrong Member",
                style=discord.ButtonStyle.secondary,
                custom_id=f"clan_links:not_them:{player_tag}",
            )
            not_them_button.callback = self._link
            self.add_item(not_them_button)
        else:
            link_button = discord.ui.Button(
                label="Choose Member",
                style=discord.ButtonStyle.success,
                custom_id=f"clan_links:link:{player_tag}",
            )
            link_button.callback = self._link
            self.add_item(link_button)

        ignore_button = discord.ui.Button(
            label="Ignore",
            style=discord.ButtonStyle.danger,
            custom_id=f"clan_links:ignore:{player_tag}",
        )
        ignore_button.callback = self._ignore
        self.add_item(ignore_button)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        await self.cog.confirm_suggestion(interaction, self.player_tag)

    async def _link(self, interaction: discord.Interaction) -> None:
        await self.cog.open_correction_flow(interaction, self.player_tag)

    async def _ignore(self, interaction: discord.Interaction) -> None:
        await self.cog.open_ignore_confirmation(interaction, self.player_tag)


class SuggestionCorrectionSelect(discord.ui.UserSelect):
    def __init__(self, cog: "AccountLinks", player_tag: str):
        super().__init__(
            placeholder="Choose the Discord member who owns this account",
            min_values=1,
            max_values=1,
        )
        self.cog = cog
        self.player_tag = player_tag

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = self.values[0] if self.values else None
        if not isinstance(selected, (discord.Member, discord.User)):
            await interaction.response.send_message("No member selected.", ephemeral=True)
            return
        await self.cog.correct_suggestion(interaction, self.player_tag, selected.id)


class SuggestionCorrectionView(BaseTimeoutView):
    def __init__(self, cog: "AccountLinks", player_tag: str):
        super().__init__(timeout=300)
        self.add_item(SuggestionCorrectionSelect(cog, player_tag))


class IgnoreSuggestionView(BaseTimeoutView):
    def __init__(self, cog: "AccountLinks", player_tag: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.player_tag = player_tag

        confirm_button = discord.ui.Button(
            label="Ignore Account",
            style=discord.ButtonStyle.danger,
        )
        confirm_button.callback = self._confirm
        self.add_item(confirm_button)

        cancel_button = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
        )
        cancel_button.callback = self._cancel
        self.add_item(cancel_button)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        await self.cog.ignore_suggestion(interaction, self.player_tag)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="Ignore cancelled. Nothing changed.", view=None)
