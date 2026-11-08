"""Roster column selection, width controls and layout feedback."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from elbow_helper.infrastructure.discord.interactions import deny, warn
from elbow_helper.infrastructure.discord.views import BaseTimeoutView
from elbow_helper.infrastructure.discord.views import BaseErrorModal
from ..models import RosterLayout
from ..config import ROSTER_DISCORD_COLUMN_MAX_WIDTH
from ..config import ROSTER_DISCORD_COLUMN_MIN_WIDTH
from ..config import ROSTER_PLAYER_COLUMN_MAX_WIDTH
from ..config import ROSTER_PLAYER_COLUMN_MIN_WIDTH

if TYPE_CHECKING:
    from ..cog import Rosters


ROSTER_LAYOUT_PROMPT = (
    "Choose which columns appear in Discord. The Player column is always shown. Google Sheets "
    "keeps every column."
)


def _join_labels(labels: list[str]) -> str:
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return f"{', '.join(labels[:-1])}, and {labels[-1]}"


def roster_layout_columns_feedback(layout: RosterLayout) -> str:
    columns: list[str] = []
    if layout.show_townhall:
        columns.append("Town Hall")
    columns.append("Player")
    if layout.show_discord:
        columns.append("Discord username")
    if layout.show_clan:
        columns.append("Clan")
    return (
        f"Roster now shows {_join_labels(columns)}.\n"
        "Google Sheets keeps every column."
    )


def roster_layout_lengths_feedback(layout: RosterLayout) -> str:
    return (
        f"Name lengths updated: Player {layout.player_width}, "
        f"Discord {layout.discord_width}.\n"
        "Google Sheets keeps every column."
    )


class RosterLayoutSelect(discord.ui.Select):
    def __init__(self, layout: RosterLayout):
        super().__init__(
            placeholder="Choose columns to show",
            min_values=0,
            max_values=3,
            options=[
                discord.SelectOption(
                    label="Town Hall",
                    value="townhall",
                    default=layout.show_townhall,
                ),
                discord.SelectOption(
                    label="Discord username",
                    value="discord",
                    default=layout.show_discord,
                ),
                discord.SelectOption(
                    label="Clan",
                    value="clan",
                    default=layout.show_clan,
                ),
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, RosterLayoutView):
            await view.cog.update_roster_layout_columns(
                interaction,
                view.roster_id,
                set(self.values),
            )


class RosterColumnWidthsModal(BaseErrorModal, title="Set roster name lengths"):
    def __init__(self, cog: "Rosters", roster_id: int, layout: RosterLayout):
        super().__init__()
        self.cog = cog
        self.roster_id = roster_id
        self.player_width = discord.ui.TextInput(
            label=(
                f"Player names ({ROSTER_PLAYER_COLUMN_MIN_WIDTH}–"
                f"{ROSTER_PLAYER_COLUMN_MAX_WIDTH} characters)"
            ),
            default=str(layout.player_width),
            min_length=1,
            max_length=2,
        )
        self.discord_width = discord.ui.TextInput(
            label=(
                f"Discord usernames ({ROSTER_DISCORD_COLUMN_MIN_WIDTH}–"
                f"{ROSTER_DISCORD_COLUMN_MAX_WIDTH} characters)"
            ),
            default=str(layout.discord_width),
            min_length=1,
            max_length=2,
        )
        self.add_item(self.player_width)
        self.add_item(self.discord_width)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            player_width = int(str(self.player_width.value))
        except ValueError:
            await warn(
                interaction,
                f"Enter a whole number from {ROSTER_PLAYER_COLUMN_MIN_WIDTH} to "
                f"{ROSTER_PLAYER_COLUMN_MAX_WIDTH} for player names.",
            )
            return
        if not ROSTER_PLAYER_COLUMN_MIN_WIDTH <= player_width <= ROSTER_PLAYER_COLUMN_MAX_WIDTH:
            await warn(
                interaction,
                f"Enter a whole number from {ROSTER_PLAYER_COLUMN_MIN_WIDTH} to "
                f"{ROSTER_PLAYER_COLUMN_MAX_WIDTH} for player names.",
            )
            return
        try:
            discord_width = int(str(self.discord_width.value))
        except ValueError:
            await warn(
                interaction,
                f"Enter a whole number from {ROSTER_DISCORD_COLUMN_MIN_WIDTH} to "
                f"{ROSTER_DISCORD_COLUMN_MAX_WIDTH} for Discord usernames.",
            )
            return
        if not ROSTER_DISCORD_COLUMN_MIN_WIDTH <= discord_width <= ROSTER_DISCORD_COLUMN_MAX_WIDTH:
            await warn(
                interaction,
                f"Enter a whole number from {ROSTER_DISCORD_COLUMN_MIN_WIDTH} to "
                f"{ROSTER_DISCORD_COLUMN_MAX_WIDTH} for Discord usernames.",
            )
            return
        await self.cog.update_roster_layout_widths(
            interaction,
            self.roster_id,
            player_width=player_width,
            discord_width=discord_width,
        )


class RosterLayoutView(BaseTimeoutView):
    def __init__(self, cog: "Rosters", roster_id: int, layout: RosterLayout):
        super().__init__(timeout=180)
        self.cog = cog
        self.roster_id = roster_id
        self.layout = layout
        self.add_item(RosterLayoutSelect(layout))

        widths = discord.ui.Button(
            label="Edit name lengths",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        widths.callback = self.edit_widths
        self.add_item(widths)

        back = discord.ui.Button(
            label="Back",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        back.callback = self.back
        self.add_item(back)

    async def edit_widths(self, interaction: discord.Interaction) -> None:
        if not self.cog.is_lead(interaction.user):
            await deny(interaction, action="manage this roster")
            return
        await interaction.response.send_modal(
            RosterColumnWidthsModal(self.cog, self.roster_id, self.layout)
        )

    async def back(self, interaction: discord.Interaction) -> None:
        if not self.cog.is_lead(interaction.user):
            await deny(interaction, action="manage this roster")
            return
        await self.cog.show_roster_settings(interaction, self.roster_id)

