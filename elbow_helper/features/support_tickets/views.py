from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from elbow_helper.discord.views import BaseTimeoutView

from elbow_helper.configuration.roles import LEAD, RECRUITERS

from .state import load_tickets, save_tickets

if TYPE_CHECKING:
    from .cog import SupportActions


def _can_close_ticket(member: discord.Member) -> bool:
    return any(role.id in (LEAD | RECRUITERS) for role in member.roles)


class SupportTicketConfirmView(BaseTimeoutView):
    def __init__(self, cog: "SupportActions"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Open",
        style=discord.ButtonStyle.secondary,
        emoji="🔓",
        custom_id="support_ticket_reopen",
    )
    async def reopen_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _can_close_ticket(interaction.user):
            await interaction.response.send_message("You don't have permission to use these controls.", ephemeral=True)
            return
        await self.cog._reopen_ticket(interaction)

    @discord.ui.button(
        label="Delete",
        style=discord.ButtonStyle.secondary,
        emoji="⛔",
        custom_id="support_ticket_delete",
    )
    async def delete_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _can_close_ticket(interaction.user):
            await interaction.response.send_message("You don't have permission to use these controls.", ephemeral=True)
            return
        await interaction.response.send_message("Deleting this ticket...", ephemeral=True)
        tickets = load_tickets()
        tickets.pop(str(interaction.channel.id), None)
        save_tickets(tickets)
        await interaction.channel.delete()


class SupportTicketCloseView(BaseTimeoutView):
    def __init__(self, cog: "SupportActions"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, emoji="🔒", custom_id="support_ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._handle_close_ticket(interaction)
