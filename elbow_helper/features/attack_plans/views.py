from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from elbow_helper.discord.pagination import NEXT_PAGE_LABEL
from elbow_helper.discord.pagination import PREV_PAGE_LABEL
from elbow_helper.discord.views import BaseTimeoutView

from .constants import LOGGER
from .formatting import ARMY_SECTION_LABELS

if TYPE_CHECKING:
    from .formatting import PlanningEmbeds


class ArmySectionSelect(discord.ui.Select):
    def __init__(self, view: "PlanningView"):
        self._parent_view = view
        options = [
            discord.SelectOption(label=ARMY_SECTION_LABELS[key], value=key)
            for key in view.army_sections
        ]
        super().__init__(
            placeholder="Choose an army view",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self._parent_view.army_section = self.values[0]
        await self._parent_view._show(interaction)


class PlanningView(BaseTimeoutView):
    """Paginator for planning embeds with a filtered Army Kit page."""

    def __init__(self, planning_embeds: "PlanningEmbeds"):
        super().__init__(timeout=86400)
        self.planning_embeds = planning_embeds
        self.page_labels = planning_embeds.page_labels
        self.index = 0
        self.army_sections = planning_embeds.army_sections()
        self.army_section = planning_embeds.default_army_section()
        self.army_select = ArmySectionSelect(self)
        self._update_controls()

    def _current_embed(self) -> discord.Embed:
        return self.planning_embeds.embed_for_page(self.index, self.army_section)

    def _update_controls(self) -> None:
        total_pages = len(self.page_labels)
        self.prev_button.disabled = self.index == 0
        self.next_button.disabled = self.index >= total_pages - 1
        self.page_button.label = f"{self.page_labels[self.index]} {self.index + 1}/{total_pages}"

        on_army_page = self.index == total_pages - 1
        if on_army_page:
            self.army_select.disabled = len(self.army_sections) <= 1
            self.army_select.placeholder = ARMY_SECTION_LABELS[self.army_section]
            for option in self.army_select.options:
                option.default = option.value == self.army_section
            if self.army_select not in self.children:
                self.add_item(self.army_select)
        elif self.army_select in self.children:
            self.remove_item(self.army_select)

    async def _show(self, interaction: discord.Interaction) -> None:
        self._update_controls()
        current_embed = self._current_embed()
        try:
            if interaction.response.is_done():
                if interaction.message:
                    await interaction.message.edit(embed=current_embed, view=self)
                return
            await interaction.response.edit_message(embed=current_embed, view=self)
        except discord.NotFound as exc:
            if getattr(exc, "code", None) == 10062 and interaction.message:
                try:
                    await interaction.message.edit(embed=current_embed, view=self)
                except (discord.NotFound, discord.HTTPException):
                    LOGGER.debug("Planning paginator message update skipped; message no longer available.")
                return
            raise

    @discord.ui.button(label=PREV_PAGE_LABEL, style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.index > 0:
            self.index -= 1
        await self._show(interaction)

    @discord.ui.button(label="Overview 1/3", style=discord.ButtonStyle.secondary, disabled=True)
    async def page_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        return

    @discord.ui.button(label=NEXT_PAGE_LABEL, style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.index < len(self.page_labels) - 1:
            self.index += 1
        await self._show(interaction)
