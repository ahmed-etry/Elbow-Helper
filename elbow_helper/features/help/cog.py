from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from elbow_helper.discord.interactions import send_bound_view, send_ephemeral

from elbow_helper.configuration.guild import GUILD_ID

from .catalog import HELP_ENTRIES, HELP_INDEX, HelpEntry
from .discovery import discover_commands
from .rendering import build_detail_embed
from .views import HelpPaginationView

LOGGER = logging.getLogger(__name__)


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.page_size = 8

    @staticmethod
    def _member_role_ids(interaction: discord.Interaction) -> set[int]:
        roles = getattr(interaction.user, "roles", ())
        return {role.id for role in roles}

    @staticmethod
    def _normalize_path(raw_command: str) -> str:
        candidate = raw_command.strip()
        if not candidate:
            return candidate
        if not candidate.startswith("/"):
            candidate = f"/{candidate}"
        return candidate

    def _visible_entries(self, interaction: discord.Interaction) -> list[HelpEntry]:
        role_ids = self._member_role_ids(interaction)
        visible: list[HelpEntry] = []
        for entry in HELP_ENTRIES:
            if entry.visible_to is None:
                visible.append(entry)
                continue
            if role_ids & entry.visible_to:
                visible.append(entry)
        return visible

    async def _show_list(self, interaction: discord.Interaction) -> None:
        entries = self._visible_entries(interaction)
        if not entries:
            await interaction.response.send_message("There are no commands available for you right now.", ephemeral=True)
            return
        view = HelpPaginationView(entries, self.page_size)
        await send_bound_view(interaction, embed=view.current_embed(), view=view, ephemeral=True)

    async def _show_detail(
        self,
        interaction: discord.Interaction,
        requested_command: str,
    ) -> None:
        normalized = self._normalize_path(requested_command)
        entry = HELP_INDEX.get(normalized)
        if entry is None:
            await interaction.response.send_message(f"Couldn't find a help page for `{requested_command}`.", ephemeral=True)
            return

        visible_entries = self._visible_entries(interaction)
        if entry not in visible_entries:
            await interaction.response.send_message("That command isn't available for your roles.", ephemeral=True)
            return

        discovered = discover_commands(self.bot)
        embed = build_detail_embed(entry, discovered.get(entry.path))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="help", description="Browse commands or get help using one.")
    @app_commands.describe(command="Command you want help with.")
    async def help(self, interaction: discord.Interaction, command: str | None = None) -> None:
        try:
            if command:
                await self._show_detail(interaction, command)
                return
            await self._show_list(interaction)
        except (discord.HTTPException, KeyError, TypeError, ValueError, RuntimeError):
            LOGGER.exception("/help failed")
            try:
                await send_ephemeral(interaction, "I couldn't load help right now. Try again in a moment.")
            except discord.HTTPException:
                LOGGER.exception("Failed to send help error response")

    def _autocomplete_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        entries = self._visible_entries(interaction)
        names = [entry.path for entry in entries]
        if current:
            lowered = current.lower()
            names = [name for name in names if lowered in name.lower()]
        deduped: list[str] = []
        seen: set[str] = set()
        for name in names:
            if name not in seen:
                deduped.append(name)
                seen.add(name)
        return [app_commands.Choice(name=name, value=name) for name in deduped[:25]]

    @help.autocomplete("command")
    async def help_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return self._autocomplete_choices(interaction, current=current)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCog(bot), guild=discord.Object(id=GUILD_ID))
