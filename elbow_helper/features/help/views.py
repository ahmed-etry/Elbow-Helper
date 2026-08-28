from __future__ import annotations

import discord
from elbow_helper.discord.pagination import ADAPTIVE_JUMP_THRESHOLD
from elbow_helper.discord.pagination import FIRST_PAGE_LABEL
from elbow_helper.discord.pagination import LAST_PAGE_LABEL
from elbow_helper.discord.pagination import NEXT_PAGE_LABEL
from elbow_helper.discord.pagination import PREV_PAGE_LABEL
from elbow_helper.discord.views import BaseTimeoutView

from .catalog import CATEGORY_ORDER, HelpEntry
from .rendering import build_list_embed

ALL_CATEGORIES_VALUE = "__all__"


class HelpCategorySelect(discord.ui.Select):
    def __init__(self, categories: list[str]) -> None:
        self.categories = categories
        super().__init__(
            placeholder="Browse by category",
            min_values=1,
            max_values=1,
            options=self._build_options(current_category=None),
            row=0,
        )

    def _build_options(self, current_category: str | None) -> list[discord.SelectOption]:
        options = [
            discord.SelectOption(
                label="All",
                value=ALL_CATEGORIES_VALUE,
                default=current_category is None,
            )
        ]
        options.extend(
            discord.SelectOption(
                label=category,
                value=category,
                default=category == current_category,
            )
            for category in self.categories
        )
        return options

    def sync(self, current_category: str | None) -> None:
        self.options = self._build_options(current_category)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not isinstance(self.view, HelpPaginationView):
            return

        selected = self.values[0]
        self.view.current_category = None if selected == ALL_CATEGORIES_VALUE else selected
        self.view.current_page = 0
        self.view.refresh()
        await interaction.response.edit_message(embed=self.view.current_embed(), view=self.view)


class HelpPaginationView(BaseTimeoutView):
    def __init__(self, entries: list[HelpEntry], page_size: int) -> None:
        super().__init__(timeout=300)
        self.entries = entries
        self.page_size = page_size
        self.current_page = 0
        self.current_category: str | None = None
        self.categories = [
            category
            for category in CATEGORY_ORDER
            if any(entry.category == category for entry in entries)
        ]
        self.category_select: HelpCategorySelect | None = None
        self.total_pages = 1
        self._jump_buttons = (self.first_page, self.last_page)
        self._nav_buttons = (self.prev_page, self.next_page)

        if len(self.categories) > 1:
            self.category_select = HelpCategorySelect(self.categories)
            self.add_item(self.category_select)

        self.refresh()

    def _filtered_entries(self) -> list[HelpEntry]:
        if self.current_category is None:
            return self.entries
        return [entry for entry in self.entries if entry.category == self.current_category]

    def _current_title(self) -> str:
        return self.current_category or "Commands You Can Use"

    def _toggle_buttons(self, buttons: tuple[discord.ui.Button[BaseTimeoutView], ...], visible: bool) -> None:
        for button in buttons:
            if visible and button not in self.children:
                self.add_item(button)
            elif not visible and button in self.children:
                self.remove_item(button)

    def _sync_buttons(self) -> None:
        self._toggle_buttons(self._nav_buttons, self.total_pages > 1)
        self._toggle_buttons(self._jump_buttons, self.total_pages > ADAPTIVE_JUMP_THRESHOLD)
        if self.total_pages <= 1:
            return

        at_start = self.current_page == 0
        at_end = self.current_page >= self.total_pages - 1
        self.first_page.disabled = at_start
        self.prev_page.disabled = at_start
        self.next_page.disabled = at_end
        self.last_page.disabled = at_end

    def refresh(self) -> None:
        filtered = self._filtered_entries()
        self.total_pages = max(1, (len(filtered) + self.page_size - 1) // self.page_size)
        if self.current_page >= self.total_pages:
            self.current_page = self.total_pages - 1
        if self.category_select is not None:
            self.category_select.sync(self.current_category)
        self._sync_buttons()

    def current_embed(self) -> discord.Embed:
        return build_list_embed(
            self._filtered_entries(),
            self.current_page,
            self.page_size,
            self._current_title(),
        )

    @discord.ui.button(label=FIRST_PAGE_LABEL, style=discord.ButtonStyle.gray, row=1)
    async def first_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.current_page = 0
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label=PREV_PAGE_LABEL, style=discord.ButtonStyle.primary, row=1)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.current_page > 0:
            self.current_page -= 1
            self._sync_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label=NEXT_PAGE_LABEL, style=discord.ButtonStyle.primary, row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self._sync_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label=LAST_PAGE_LABEL, style=discord.ButtonStyle.gray, row=1)
    async def last_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.current_page = self.total_pages - 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)
