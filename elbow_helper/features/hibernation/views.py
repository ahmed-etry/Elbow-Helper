from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from elbow_helper.discord.views import BaseTimeoutView

from elbow_helper.configuration.roles import CORE, LEAD, RECRUITERS

if TYPE_CHECKING:
    from .cog import Hibernate


def _has_close_perms(member: discord.Member) -> bool:
    return any(role.id in (RECRUITERS | LEAD | CORE) for role in member.roles)


class CloseTicketView(BaseTimeoutView):
    def __init__(self, cog: "Hibernate"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Close",
        style=discord.ButtonStyle.secondary,
        emoji="🔒",
        custom_id="hibernation_close_ticket",
    )
    async def close_ticket(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not _has_close_perms(interaction.user):
            await interaction.response.send_message("You don't have permission to close this ticket.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.cog._close_reactivation_ticket(interaction)


class CloseTicketConfirmView(BaseTimeoutView):
    def __init__(self, cog: "Hibernate"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Open",
        style=discord.ButtonStyle.secondary,
        emoji="🔓",
        custom_id="hibernation_ticket_reopen",
    )
    async def reopen_ticket(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not _has_close_perms(interaction.user):
            await interaction.response.send_message("You don't have permission to use these controls.", ephemeral=True)
            return
        await self.cog._reopen_reactivation_ticket(interaction)

    @discord.ui.button(
        label="Delete",
        style=discord.ButtonStyle.secondary,
        emoji="⛔",
        custom_id="hibernation_ticket_delete",
    )
    async def delete_ticket(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not _has_close_perms(interaction.user):
            await interaction.response.send_message("You don't have permission to use these controls.", ephemeral=True)
            return
        await interaction.response.send_message("Deleting this ticket...", ephemeral=True)
        await interaction.channel.delete()


class ReactivateView(BaseTimeoutView):
    def __init__(self, cog: "Hibernate"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Reactivate Me",
        style=discord.ButtonStyle.success,
        custom_id="hibernation_reactivate_me",
    )
    async def reactivate_me(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.reactivate_from_button(interaction)
