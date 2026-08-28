"""Interactive views for raffle hub and achievement overview pagination."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from elbow_helper.discord.pagination import ADAPTIVE_JUMP_THRESHOLD
from elbow_helper.discord.pagination import FIRST_PAGE_LABEL
from elbow_helper.discord.pagination import format_page_footer
from elbow_helper.discord.pagination import LAST_PAGE_LABEL
from elbow_helper.discord.pagination import NEXT_PAGE_LABEL
from elbow_helper.discord.pagination import PREV_PAGE_LABEL
from elbow_helper.discord.views import BaseTimeoutView

if TYPE_CHECKING:
    from .cog import Achievements


class RaffleHubView(BaseTimeoutView):
    def __init__(self, cog: "Achievements", buy_enabled: bool = True):
        super().__init__(timeout=None)
        self.cog = cog

        buy_button = discord.ui.Button(
            label="Buy Ticket",
            style=discord.ButtonStyle.success,
            custom_id="raffle_hub:buy_ticket",
        )
        buy_button.disabled = not buy_enabled
        buy_button.callback = self.buy_ticket
        self.add_item(buy_button)

        inventory_button = discord.ui.Button(
            label="Inventory",
            style=discord.ButtonStyle.secondary,
            custom_id="raffle_hub:inventory",
        )
        inventory_button.callback = self.inventory
        self.add_item(inventory_button)

    async def buy_ticket(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        ok, msg = await self.cog._retry_db_operation(
            self.cog._buy_ticket_internal,
            interaction.user.id,
        )
        await interaction.followup.send(msg, ephemeral=True)
        if ok:
            await self.cog.update_raffle_hub_message()

    async def inventory(self, interaction: discord.Interaction):
        balance, has_ticket = await self.cog._retry_db_operation(
            self.cog._get_inventory_internal,
            interaction.user.id,
        )
        embed = self.cog._build_inventory_embed(interaction.user, balance, has_ticket)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class CoinLogView(BaseTimeoutView):
    def __init__(self, embeds: list[discord.Embed]):
        super().__init__(timeout=300)
        self.embeds = embeds
        self.page = 0
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        self.previous_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= len(self.embeds) - 1

    async def _render(self, interaction: discord.Interaction) -> None:
        self.page = max(0, min(self.page, len(self.embeds) - 1))
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.page], view=self)

    @discord.ui.button(label=PREV_PAGE_LABEL, style=discord.ButtonStyle.gray)
    async def previous_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.page -= 1
        await self._render(interaction)

    @discord.ui.button(label=NEXT_PAGE_LABEL, style=discord.ButtonStyle.gray)
    async def next_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.page += 1
        await self._render(interaction)


class AchievementOverviewView(BaseTimeoutView):
    """Pagination with section toggles for /achievements."""

    def __init__(self, in_progress_pages: list, completed_pages: list):
        super().__init__(timeout=300)
        self.sections = {
            "in_progress": in_progress_pages,
            "completed": completed_pages,
        }
        self.current_section = "in_progress"
        self.current_page = 0
        self._refresh_button_states()

    def _current_pages(self) -> list:
        return self.sections[self.current_section]

    def _jump_buttons_visible(self) -> bool:
        return len(self._current_pages()) > ADAPTIVE_JUMP_THRESHOLD

    def _refresh_jump_buttons(self) -> None:
        for button in (self.first_page, self.last_page):
            if self._jump_buttons_visible() and button not in self.children:
                self.add_item(button)
            elif not self._jump_buttons_visible() and button in self.children:
                self.remove_item(button)

    def _refresh_button_states(self):
        pages = self._current_pages()
        self._refresh_jump_buttons()
        self.first_page.disabled = self.current_page == 0
        self.previous_page.disabled = self.current_page == 0
        self.next_page.disabled = self.current_page >= len(pages) - 1
        self.last_page.disabled = self.current_page >= len(pages) - 1
        self.show_in_progress.disabled = self.current_section == "in_progress"
        self.show_completed.disabled = self.current_section == "completed"

    def _page_footer(self, page_idx: int, total: int) -> str:
        section_label = "In Progress" if self.current_section == "in_progress" else "Completed"
        return format_page_footer(page_idx + 1, total, section=section_label)

    def _set_embed_footer(self, embed: discord.Embed, page_idx: int, total: int):
        embed.set_footer(text=self._page_footer(page_idx, total))

    async def _render(self, interaction: discord.Interaction):
        pages = self._current_pages()
        page_idx = max(0, min(self.current_page, len(pages) - 1))
        self.current_page = page_idx
        embed = pages[page_idx]
        self._set_embed_footer(embed, page_idx, len(pages))
        self._refresh_button_states()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label=FIRST_PAGE_LABEL, style=discord.ButtonStyle.gray)
    async def first_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = 0
        await self._render(interaction)

    @discord.ui.button(label=PREV_PAGE_LABEL, style=discord.ButtonStyle.gray, disabled=True)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page -= 1
        await self._render(interaction)

    @discord.ui.button(label=NEXT_PAGE_LABEL, style=discord.ButtonStyle.gray)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page += 1
        await self._render(interaction)

    @discord.ui.button(label=LAST_PAGE_LABEL, style=discord.ButtonStyle.gray)
    async def last_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = len(self._current_pages()) - 1
        await self._render(interaction)

    @discord.ui.button(label="In Progress", style=discord.ButtonStyle.blurple)
    async def show_in_progress(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_section = "in_progress"
        self.current_page = 0
        await self._render(interaction)

    @discord.ui.button(label="Completed", style=discord.ButtonStyle.green)
    async def show_completed(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_section = "completed"
        self.current_page = 0
        await self._render(interaction)
