"""Board view for role connections panels."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from elbow_helper.discord.interactions import deny
from elbow_helper.discord.interactions import send_bound_view
from elbow_helper.discord.pagination import NEXT_PAGE_LABEL
from elbow_helper.discord.pagination import PREV_PAGE_LABEL
from elbow_helper.discord.views import BaseTimeoutView

from .builder import TargetRoleSelectView
from .edit import EditConnectionView
from .edit import RemoveConnectionView
from .view_utils import parse_page_from_footer

if TYPE_CHECKING:
    from .cog import RoleConnections


class ConnectionsView(BaseTimeoutView):
    def __init__(self, cog: "RoleConnections"):
        super().__init__(timeout=1800)
        self.cog = cog

        add_button = discord.ui.Button(
            label="Add",
            style=discord.ButtonStyle.success,
            custom_id="roleconn:add",
        )
        add_button.callback = self.add_connection
        self.add_item(add_button)

        remove_button = discord.ui.Button(
            label="Remove",
            style=discord.ButtonStyle.danger,
            custom_id="roleconn:remove",
        )
        remove_button.callback = self.remove_connection
        self.add_item(remove_button)

        edit_button = discord.ui.Button(
            label="Edit",
            style=discord.ButtonStyle.primary,
            custom_id="roleconn:edit",
        )
        edit_button.callback = self.edit_connection
        self.add_item(edit_button)

        scan_button = discord.ui.Button(
            label="Apply Roles",
            style=discord.ButtonStyle.secondary,
            custom_id="roleconn:scan",
        )
        scan_button.callback = self.scan_connections
        self.add_item(scan_button)

        self.prev_button = discord.ui.Button(
            label=PREV_PAGE_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="roleconn:prev",
        )
        self.prev_button.callback = self.prev_page
        self.add_item(self.prev_button)

        self.next_button = discord.ui.Button(
            label=NEXT_PAGE_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="roleconn:next",
        )
        self.next_button.callback = self.next_page
        self.add_item(self.next_button)

        self._set_pagination_state(0)

    def _set_pagination_state(self, page_index: int) -> None:
        total_pages = self.cog.get_connections_page_count()
        self.prev_button.disabled = page_index <= 0
        self.next_button.disabled = page_index >= total_pages - 1

    @staticmethod
    def _interaction_channel(interaction: discord.Interaction) -> discord.TextChannel | None:
        channel = interaction.channel
        return channel if isinstance(channel, discord.TextChannel) else None

    async def prev_page(self, interaction: discord.Interaction) -> None:
        current, _ = parse_page_from_footer(
            interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None,
            self.cog.get_connections_page_count(),
        )
        new_page = max(0, current - 1)
        embed = self.cog.build_connections_embed(page=new_page)
        self._set_pagination_state(new_page)
        await interaction.response.edit_message(embed=embed, view=self)

    async def next_page(self, interaction: discord.Interaction) -> None:
        current, total = parse_page_from_footer(
            interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None,
            self.cog.get_connections_page_count(),
        )
        new_page = min(total - 1, current + 1)
        embed = self.cog.build_connections_embed(page=new_page)
        self._set_pagination_state(new_page)
        await interaction.response.edit_message(embed=embed, view=self)

    async def add_connection(self, interaction: discord.Interaction) -> None:
        if not self.cog._can_manage(interaction.user):
            await deny(interaction)
            return
        channel = self._interaction_channel(interaction)
        if channel is None:
            await interaction.response.send_message("Use this in a server channel.", ephemeral=True)
            return
        view = TargetRoleSelectView(self.cog, channel)
        await send_bound_view(interaction, embed=view.build_embed(), view=view, ephemeral=True)

    async def remove_connection(self, interaction: discord.Interaction) -> None:
        if not self.cog._can_manage(interaction.user):
            await deny(interaction)
            return
        if not self.cog.state["connections"]:
            await interaction.response.send_message("No role connections to remove.", ephemeral=True)
            return
        channel = self._interaction_channel(interaction)
        if channel is None:
            await interaction.response.send_message("Use this in a server channel.", ephemeral=True)
            return
        view = RemoveConnectionView(self.cog, channel, page=0)
        await send_bound_view(interaction, embed=view.build_embed(), view=view, ephemeral=True)

    async def edit_connection(self, interaction: discord.Interaction) -> None:
        if not self.cog._can_manage(interaction.user):
            await deny(interaction)
            return
        if not self.cog.state["connections"]:
            await interaction.response.send_message("No role connections to edit.", ephemeral=True)
            return
        channel = self._interaction_channel(interaction)
        if channel is None:
            await interaction.response.send_message("Use this in a server channel.", ephemeral=True)
            return
        view = EditConnectionView(self.cog, channel, page=0)
        await send_bound_view(interaction, embed=view.build_embed(), view=view, ephemeral=True)

    async def scan_connections(self, interaction: discord.Interaction) -> None:
        if not self.cog._can_manage(interaction.user):
            await deny(interaction)
            return
        await self.cog.post_scan_preview(interaction)

