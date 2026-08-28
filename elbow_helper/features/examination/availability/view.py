"""Applicant availability prompt UI."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import discord
from elbow_helper.discord.interactions import send_bound_view
from elbow_helper.discord.views import BaseErrorModal
from elbow_helper.discord.views import BaseTimeoutView

from .logic import _format_structured_availability_display
from .logic import _parse_single_time_input
from ..config import AVAILABILITY_DAY_OPTIONS
from ..config import TIMEZONE_SELECT_OPTIONS

if TYPE_CHECKING:
    from ..cog import Examination


class AvailabilityTimeModal(BaseErrorModal):
    def __init__(self, view: "AvailabilityPromptView"):
        super().__init__(title="Set Availability Time Range")
        self.view = view
        self.start_time = discord.ui.TextInput(
            label="Start Time",
            placeholder="e.g. 07:00, 7am, 1630",
            required=True,
            max_length=10,
        )
        self.end_time = discord.ui.TextInput(
            label="End Time",
            placeholder="e.g. 17:00, 5pm, 2230",
            required=True,
            max_length=10,
        )
        self.add_item(self.start_time)
        self.add_item(self.end_time)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.view._check(interaction):
            await interaction.response.send_message("Only the applicant can set their availability.", ephemeral=True)
            return
        start = _parse_single_time_input(self.start_time.value)
        end = _parse_single_time_input(self.end_time.value)
        if start is None or end is None:
            await interaction.response.send_message(
                "That time wasn't recognized. Try formats like 16:00, 4pm, 1630, noon, or midnight.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        self.view.cog._update_availability_draft(
            interaction.channel_id,
            start=start,
            end=end,
            user_id=interaction.user.id,
        )
        await self.view.cog._update_availability_prompt(interaction.channel_id)


class AvailabilityPromptView(BaseTimeoutView):
    def __init__(self, cog: Examination, channel_id: Optional[int] = None):
        super().__init__(timeout=None)
        self.cog = cog
        self.channel_id = channel_id
        default_tz = None
        if channel_id:
            case = self.cog._get_case(channel_id)
            if case:
                draft = case.get("availability_draft") or {}
                default_tz = draft.get("timezone")
                if not default_tz:
                    windows = self.cog._get_normalized_availability_windows(case)
                    if windows:
                        default_tz = windows[-1].get("timezone")
        if default_tz:
            for child in self.children:
                if isinstance(child, discord.ui.Select) and child.custom_id == "exam_availability_timezone":
                    for option in child.options:
                        option.default = option.value == default_tz

    def _check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        case = self.cog._get_case(interaction.channel_id)
        if not case:
            return False
        opener_id = case.get("opener_id")
        if opener_id and interaction.user.id != opener_id:
            return False
        return True

    @discord.ui.select(
        placeholder="Select available days",
        custom_id="exam_availability_days",
        min_values=1,
        max_values=7,
        row=0,
        options=AVAILABILITY_DAY_OPTIONS,
    )
    async def days_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not self._check(interaction):
            await interaction.response.send_message("Only the applicant can set their availability.", ephemeral=True)
            return
        days = {value for value in select.values}
        await interaction.response.defer()
        self.cog._update_availability_draft(
            interaction.channel_id,
            days=days,
            user_id=interaction.user.id,
        )
        await self.cog._update_availability_prompt(interaction.channel_id)

    @discord.ui.select(
        placeholder="Select timezone",
        custom_id="exam_availability_timezone",
        min_values=1,
        max_values=1,
        row=1,
        options=TIMEZONE_SELECT_OPTIONS,
    )
    async def timezone_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not self._check(interaction):
            await interaction.response.send_message("Only the applicant can set their availability.", ephemeral=True)
            return
        await interaction.response.defer()
        self.cog._update_availability_draft(
            interaction.channel_id,
            timezone_text=select.values[0],
            user_id=interaction.user.id,
        )
        await self.cog._update_availability_prompt(interaction.channel_id)

    @discord.ui.button(
        label="Set Time Range",
        style=discord.ButtonStyle.secondary,
        custom_id="exam_availability_time",
        row=3,
    )
    async def set_time(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check(interaction):
            await interaction.response.send_message("Only the applicant can set their availability.", ephemeral=True)
            return
        await interaction.response.send_modal(AvailabilityTimeModal(self))

    @discord.ui.button(
        label="Clear Selection",
        style=discord.ButtonStyle.secondary,
        custom_id="exam_availability_reset_draft",
        row=3,
    )
    async def reset_draft(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check(interaction):
            await interaction.response.send_message("Only the applicant can edit their availability.", ephemeral=True)
            return
        cleared = self.cog._clear_availability_draft(interaction.channel_id, user_id=interaction.user.id)
        if not cleared:
            await interaction.response.send_message("There is no unfinished availability to clear.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.cog._update_availability_prompt(interaction.channel_id)

    @discord.ui.button(
        label="Add Window",
        style=discord.ButtonStyle.primary,
        custom_id="exam_availability_add_window",
        row=4,
    )
    async def add_window(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check(interaction):
            await interaction.response.send_message("Only the applicant can set their availability.", ephemeral=True)
            return
        added = self.cog._add_availability_window(interaction.channel_id, user_id=interaction.user.id)
        if not added:
            await interaction.response.send_message(
                "Choose at least one day, a timezone, and a time range before adding the window.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        await self.cog._update_availability_prompt(interaction.channel_id)

    @discord.ui.button(
        label="Remove Window",
        style=discord.ButtonStyle.secondary,
        custom_id="exam_availability_remove_window",
        row=4,
    )
    async def remove_window(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check(interaction):
            await interaction.response.send_message("Only the applicant can edit their availability.", ephemeral=True)
            return
        case = self.cog._get_case(interaction.channel_id)
        windows = self.cog._get_normalized_availability_windows(case or {})
        if not windows:
            await interaction.response.send_message("No saved windows to remove yet.", ephemeral=True)
            return
        view = AvailabilityRemoveWindowView(self.cog, interaction.channel_id, windows)
        await send_bound_view(
            interaction,
            content="Choose a saved window to remove.",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Finish",
        style=discord.ButtonStyle.success,
        custom_id="exam_availability_finish",
        row=4,
    )
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check(interaction):
            await interaction.response.send_message("Only the applicant can set their availability.", ephemeral=True)
            return
        availability = self.cog._finalize_availability(interaction.channel_id, user_id=interaction.user.id)
        if not availability:
            await interaction.response.send_message("Add at least one availability window first.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.cog._finalize_availability_prompt(interaction.channel_id)
        asyncio.create_task(self.cog._maybe_route_after_availability(interaction.channel_id))


class AvailabilityRemoveWindowSelect(discord.ui.Select):
    def __init__(self, view: "AvailabilityRemoveWindowView", windows: List[Dict[str, Any]]):
        self._parent_view = view
        options: List[discord.SelectOption] = []
        for index, window in enumerate(windows, start=1):
            summary = _format_structured_availability_display([window]) or f"Window {index}"
            options.append(
                discord.SelectOption(
                    label=f"Window {index}",
                    value=str(index - 1),
                    description=summary[:100],
                )
            )
        super().__init__(
            placeholder="Select a saved window to remove",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        index = int(self.values[0])
        removed = self._parent_view.cog._remove_availability_window(
            self._parent_view.channel_id,
            index,
            user_id=interaction.user.id,
        )
        if not removed:
            self._parent_view.stop()
            await interaction.response.edit_message(
                content="That availability window no longer exists.",
                view=None,
            )
            return
        self._parent_view.stop()
        await interaction.response.edit_message(
            content=f"Removed window {index + 1}.",
            view=None,
        )
        await self._parent_view.cog._update_availability_prompt(self._parent_view.channel_id)


class AvailabilityRemoveWindowView(BaseTimeoutView):
    def __init__(self, cog: Examination, channel_id: int, windows: List[Dict[str, Any]]):
        super().__init__(timeout=180)
        self.cog = cog
        self.channel_id = channel_id
        self.add_item(AvailabilityRemoveWindowSelect(self, windows))
