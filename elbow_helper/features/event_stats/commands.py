"""Slash command surface for event stats."""

from __future__ import annotations

import discord
from discord import app_commands
from elbow_helper.discord.interactions import deny
from elbow_helper.discord.interactions import send_bound_view

from elbow_helper.configuration.guild import GUILD_ID

from .views import EventListView
from .views import EventPanelView


class EventStatsCommandsMixin:
    event_group = app_commands.Group(name="event", description="Track event schedules and participation.", guild_ids=[GUILD_ID])

    @event_group.command(name="panel", description="Create, edit, or organize event trackers.")
    async def event_panel(self, interaction: discord.Interaction) -> None:
        if not self._can_manage(interaction.user):
            await deny(interaction)
            return

        guild = interaction.guild or self._get_guild()
        view = EventPanelView(self, guild)
        await send_bound_view(
            interaction,
            embed=self.build_panel_embed(guild),
            view=view,
            ephemeral=True,
        )

    @event_group.command(name="list", description="See every event tracker and its current status.")
    async def event_list(self, interaction: discord.Interaction) -> None:
        if not self._can_manage(interaction.user):
            await deny(interaction)
            return

        guild = interaction.guild or self._get_guild()
        view = EventListView(self, guild, page=0)
        await send_bound_view(
            interaction,
            embed=view.build_embed(),
            view=view,
            ephemeral=True,
        )

    @event_group.command(name="update", description="Update every event count now.")
    async def event_update(self, interaction: discord.Interaction) -> None:
        if not self._can_manage(interaction.user):
            await deny(interaction)
            return

        guild = interaction.guild or self._get_guild()
        if guild is None:
            await interaction.response.send_message(
                "I couldn't reach the server right now. Try again in a moment.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        await self.force_refresh(guild)
        await interaction.followup.send("Event stats are up to date.", ephemeral=True)
