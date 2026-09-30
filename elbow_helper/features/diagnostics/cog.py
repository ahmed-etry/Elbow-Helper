from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import MissingAnyRole
from elbow_helper.discord.interactions import deny

from elbow_helper.domain.player_tags import encode_clash_tag
from elbow_helper.infrastructure.clash import ClashClient
from elbow_helper.configuration.clans import CLAN_TAGS
from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.configuration.roles import CORE

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ClashConnectionCheck:
    outcome: str
    clan_name: str = ""
    clan_tag: str = ""
    response: Any = None
    embed: discord.Embed | None = None
    message: str = ""


class DebugCog(commands.Cog):
    """Debug utilities and shared command error handling."""

    def __init__(self, bot: commands.Bot, clash_client: ClashClient):
        self.bot = bot
        self.clash_client = clash_client

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if isinstance(error, MissingAnyRole):
            await ctx.send("You don't have permission to use this command.")
        else:
            raise error

    @app_commands.command(name="ping", description="Check the bot's response time.")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def ping(self, interaction: discord.Interaction):
        latency = round(self.bot.latency * 1000)
        try:
            await interaction.response.defer(ephemeral=False, thinking=False)
            await interaction.followup.send(f"Pong! Latency: `{latency}ms`", ephemeral=False)
        except discord.NotFound:
            LOGGER.warning("Ping interaction expired before response could be sent")

    @app_commands.command(name="api", description="Test Clash of Clans API connection for a clan.")
    @app_commands.describe(clan="Clan whose Clash connection you want to test. Leave empty to check BEH.")
    @app_commands.choices(clan=[app_commands.Choice(name=name, value=name) for name in CLAN_TAGS.keys()])
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def coc_api_test(self, interaction: discord.Interaction, clan: Optional[str] = None):
        if not any(role.id in CORE for role in getattr(interaction.user, "roles", [])):
            await deny(interaction)
            return
        check = await self.check_clash_connection(
            clan, on_ready=lambda: interaction.response.defer(ephemeral=False, thinking=True),
        )
        if check.outcome == "not_configured":
            await interaction.response.send_message(check.message, ephemeral=False)
            return
        if check.outcome == "invalid_clan":
            await interaction.followup.send(check.message, ephemeral=True)
            return
        if check.outcome == "failed":
            LOGGER.warning(
                "CoC API test failed: clan=%s reason=%s",
                check.clan_name,
                check.response.error,
            )
            await interaction.followup.send(check.message, ephemeral=False)
            return
        await interaction.followup.send(embed=check.embed, ephemeral=False)

    @staticmethod
    def _build_connection_embed(clan_name: str, tag: str, response: Any) -> discord.Embed:
        status = response.status
        data = response.payload_object or {}
        latency_ms = response.latency_ms
        rate_limit = {
            "limit": response.headers.get("X-RateLimit-Limit"),
            "remaining": response.headers.get("X-RateLimit-Remaining"),
            "reset": response.headers.get("X-RateLimit-Reset"),
        }

        state = data.get("state", "unknown")
        opponent = (data.get("opponent") or {}).get("name", "unknown")
        end_time = data.get("endTime")

        color = discord.Color.green() if status and status < 400 else discord.Color.red()
        embed = discord.Embed(
            title="Clash API Status",
            description=f"Current war details for {clan_name}.",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="API Status", value=str(status), inline=True)
        if latency_ms is not None:
            embed.add_field(name="Latency", value=f"{latency_ms} ms", inline=True)
        embed.add_field(name="Clan", value=f"{clan_name} ({tag})", inline=False)
        if any(rate_limit.values()):
            rl_text = (
                f"Limit: {rate_limit.get('limit') or '?'} / "
                f"Remaining: {rate_limit.get('remaining') or '?'} / "
                f"Reset: {rate_limit.get('reset') or '?'}"
            )
            embed.add_field(name="Rate Limit", value=rl_text, inline=False)

        if status and status < 400:
            embed.add_field(name="Connection", value="Reachable", inline=True)
            embed.add_field(name="War State", value=state, inline=True)
            embed.add_field(name="Opponent", value=opponent, inline=True)
            if end_time:
                embed.add_field(name="War Ends", value=end_time, inline=False)
        else:
            reason = data.get("reason") if isinstance(data, dict) else None
            embed.add_field(name="Connection", value="Unavailable", inline=True)
            if reason:
                embed.add_field(name="Reason", value=str(reason), inline=False)

        return embed

    async def check_clash_connection(
        self, clan: str | None = None,
        *, on_ready: Callable[[], Awaitable[Any]] | None = None,
    ) -> ClashConnectionCheck:
        if not self.clash_client.configured:
            return ClashConnectionCheck(
                "not_configured", message="Clash API access isn't set up yet.",
            )
        if on_ready is not None:
            await on_ready()
        clan_name = clan or next(iter(CLAN_TAGS.keys()))
        tag = CLAN_TAGS.get(clan_name)
        if not tag:
            return ClashConnectionCheck(
                "invalid_clan", clan_name=clan_name,
                message=f"`{clan_name}` isn't available for this check.",
            )
        response = await self.clash_client.get(
            f"/clans/{encode_clash_tag(tag)}/currentwar",
            attempts=1, timeout_seconds=10,
        )
        if response.error is not None:
            return ClashConnectionCheck(
                "failed", clan_name=clan_name, clan_tag=tag,
                response=response,
                message="Clash API check failed. Try again in a moment.",
            )
        return ClashConnectionCheck(
            "complete", clan_name=clan_name, clan_tag=tag,
            response=response,
            embed=self._build_connection_embed(clan_name, tag, response),
        )
