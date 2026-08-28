import asyncio
import sqlite3
from typing import Any
from typing import Protocol

import discord
from discord import app_commands
from discord.ext import commands
from elbow_helper.discord.interactions import deny, warn

from elbow_helper.domain.player_tags import normalize_player_tag
from elbow_helper.infrastructure.clash import ClashClient
from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.configuration.roles import MEMBERS, PLANNING_HELPERS

from .api import fetch_player
from .formatting import build_planning_embeds
from .views import PlanningView


class PlayerSearch(Protocol):
    def search_players(
        self,
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]: ...


class Planning(commands.Cog):
    """Attack planning helper: gathers player data and base image for review."""

    def __init__(
        self,
        bot: commands.Bot,
        clash_client: ClashClient,
        clan_health: PlayerSearch,
    ):
        self.bot = bot
        self.clash_client = clash_client
        self.clan_health = clan_health

    async def player_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        del interaction
        try:
            rows = await asyncio.wait_for(
                asyncio.to_thread(
                    self.clan_health.search_players,
                    current,
                    25,
                ),
                timeout=1.5,
            )
            return [
                app_commands.Choice(
                    name=(
                        f"{row.get('player_name') or row['player_tag']} - "
                        f"{row.get('clan_code') or '-'} - "
                        f"TH{int(row.get('townhall') or 0)} - {row['player_tag']}"
                    )[:100],
                    value=str(row["player_tag"]),
                )
                for row in rows
            ]
        except TimeoutError:
            return []
        except (KeyError, TypeError, ValueError, sqlite3.Error):
            return []

    @staticmethod
    def _player_fetch_error_text(player: dict | None) -> str:
        if not player:
            return "Clash data isn't available right now. Try again later."

        if player.get("_error") == "network_error":
            return "Clash data isn't available right now. Try again later."

        status = player.get("_http_status")
        if status == 404:
            return "That player couldn't be found."
        if status in (401, 403):
            return "Clash data is unavailable."
        if status == 429:
            return "Clash data isn't available right now. Try again later."
        if isinstance(status, int) and status >= 500:
            return "Clash data isn't available right now. Try again later."

        return "Clash data couldn't be loaded."

    @app_commands.command(name="plan", description="Get help planning an attack.")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.autocomplete(player=player_autocomplete)
    @app_commands.describe(
        player="Your Clash account for this attack plan.",
        strategies="Armies or strategies you're comfortable with or want to use.",
        base_image="Screenshot of the base you want help attacking.",
    )
    async def planning(
        self,
        interaction: discord.Interaction,
        player: str,
        strategies: str,
        base_image: discord.Attachment,
    ) -> None:
        if not any(role.id in MEMBERS for role in interaction.user.roles):
            await deny(interaction)
            return

        player_tag = normalize_player_tag(player)
        if not player_tag:
            await warn(interaction, "Choose your Clash account from the list.")
            return

        if not self.clash_client.configured:
            await interaction.response.send_message(
                "Clash data isn't available because the connection hasn't been set up.",
                ephemeral=True,
            )
            return

        if base_image.content_type and not base_image.content_type.startswith("image/"):
            await interaction.response.send_message("Base screenshot needs to be an image file.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False, thinking=True)

        player = await fetch_player(player_tag, self.clash_client)
        if not player or player.get("_http_status", 500) >= 400:
            await interaction.followup.send(self._player_fetch_error_text(player), ephemeral=True)
            return

        planning_embeds = build_planning_embeds(interaction, player, strategies, base_image)
        mention_roles = " ".join(f"<@&{role_id}>" for role_id in PLANNING_HELPERS) or None
        view = PlanningView(planning_embeds)

        review_message = await interaction.followup.send(
            content=mention_roles,
            embed=planning_embeds.embed_for_page(0),
            view=view,
            ephemeral=False,
            wait=True,
        )
        if isinstance(review_message, discord.Message):
            view.bind_message(review_message)

        await interaction.followup.send("Attack plan submitted.", ephemeral=True)
