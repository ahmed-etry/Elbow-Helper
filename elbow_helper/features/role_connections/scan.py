"""Persistent scan preview and execution views for role connections."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from elbow_helper.discord.interactions import deny
from elbow_helper.discord.pagination import ADAPTIVE_JUMP_THRESHOLD
from elbow_helper.discord.pagination import FIRST_PAGE_LABEL
from elbow_helper.discord.pagination import LAST_PAGE_LABEL
from elbow_helper.discord.pagination import NEXT_PAGE_LABEL
from elbow_helper.discord.pagination import PREV_PAGE_LABEL
from elbow_helper.discord.views import BaseTimeoutView

from .view_utils import parse_page_from_footer

LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .cog import RoleConnections


class ScanConfirmView(BaseTimeoutView):
    def __init__(self, cog: "RoleConnections", page: int = 0):
        super().__init__(timeout=900)
        self.cog = cog

        if self.cog.get_scan_preview_page_count() > ADAPTIVE_JUMP_THRESHOLD:
            self.first_button = discord.ui.Button(
                label=FIRST_PAGE_LABEL,
                style=discord.ButtonStyle.secondary,
                custom_id="roleconn:scan_first",
            )
            self.first_button.callback = self.first_page
            self.add_item(self.first_button)
        else:
            self.first_button = None

        self.prev_button = discord.ui.Button(
            label=PREV_PAGE_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="roleconn:scan_prev",
        )
        self.prev_button.callback = self.prev_page
        self.add_item(self.prev_button)

        self.next_button = discord.ui.Button(
            label=NEXT_PAGE_LABEL,
            style=discord.ButtonStyle.secondary,
            custom_id="roleconn:scan_next",
        )
        self.next_button.callback = self.next_page
        self.add_item(self.next_button)

        if self.cog.get_scan_preview_page_count() > ADAPTIVE_JUMP_THRESHOLD:
            self.last_button = discord.ui.Button(
                label=LAST_PAGE_LABEL,
                style=discord.ButtonStyle.secondary,
                custom_id="roleconn:scan_last",
            )
            self.last_button.callback = self.last_page
            self.add_item(self.last_button)
        else:
            self.last_button = None

        cancel_button = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id="roleconn:scan_cancel",
        )
        cancel_button.callback = self.cancel
        self.add_item(cancel_button)

        start_button = discord.ui.Button(
            label="Apply Now",
            style=discord.ButtonStyle.success,
            custom_id="roleconn:scan_start",
        )
        start_button.callback = self.start
        self.add_item(start_button)
        self._set_pagination_state(page)

    def _set_pagination_state(self, page_index: int, total_pages: int | None = None) -> None:
        total = total_pages or self.cog.get_scan_preview_page_count()
        if self.first_button is not None:
            self.first_button.disabled = total <= 1 or page_index <= 0
        self.prev_button.disabled = total <= 1 or page_index <= 0
        self.next_button.disabled = total <= 1 or page_index >= total - 1
        if self.last_button is not None:
            self.last_button.disabled = total <= 1 or page_index >= total - 1

    async def first_page(self, interaction: discord.Interaction) -> None:
        _, total = parse_page_from_footer(
            interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None,
            self.cog.get_scan_preview_page_count(),
        )
        embed = self.cog.build_scan_preview_embed(page=0)
        self._set_pagination_state(0, total)
        await interaction.response.edit_message(embed=embed, view=self)

    async def prev_page(self, interaction: discord.Interaction) -> None:
        current, total = parse_page_from_footer(
            interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None,
            self.cog.get_scan_preview_page_count(),
        )
        new_page = max(0, current - 1)
        embed = self.cog.build_scan_preview_embed(page=new_page)
        self._set_pagination_state(new_page, total)
        await interaction.response.edit_message(embed=embed, view=self)

    async def next_page(self, interaction: discord.Interaction) -> None:
        current, total = parse_page_from_footer(
            interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None,
            self.cog.get_scan_preview_page_count(),
        )
        new_page = min(total - 1, current + 1)
        embed = self.cog.build_scan_preview_embed(page=new_page)
        self._set_pagination_state(new_page, total)
        await interaction.response.edit_message(embed=embed, view=self)

    async def last_page(self, interaction: discord.Interaction) -> None:
        _, total = parse_page_from_footer(
            interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None,
            self.cog.get_scan_preview_page_count(),
        )
        new_page = max(0, total - 1)
        embed = self.cog.build_scan_preview_embed(page=new_page)
        self._set_pagination_state(new_page, total)
        await interaction.response.edit_message(embed=embed, view=self)

    async def cancel(self, interaction: discord.Interaction) -> None:
        if not self.cog._can_manage(interaction.user):
            await deny(interaction)
            return
        if not interaction.response.is_done():
            await interaction.response.defer()
        try:
            await interaction.message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            current, _ = parse_page_from_footer(
                interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None,
                self.cog.get_scan_preview_page_count(),
            )
            disabled = ScanConfirmView(self.cog, current)
            for item in disabled.children:
                item.disabled = True
            try:
                await interaction.message.edit(content="No role changes were made.", view=disabled)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                LOGGER.debug("Failed to update cancelled scan prompt")

    async def start(self, interaction: discord.Interaction) -> None:
        if not self.cog._can_manage(interaction.user):
            await deny(interaction)
            return
        current, _ = parse_page_from_footer(
            interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None,
            self.cog.get_scan_preview_page_count(),
        )
        disabled = ScanConfirmView(self.cog, current)
        for item in disabled.children:
            item.disabled = True
        await interaction.response.edit_message(content="Applying role connections now.", view=disabled)
        task = asyncio.create_task(self.cog.run_scan(interaction.message))
        self.cog._scan_tasks.add(task)
        task.add_done_callback(self.cog._scan_tasks.discard)

