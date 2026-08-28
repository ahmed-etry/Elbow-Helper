from __future__ import annotations

import asyncio
import logging
import re

import discord
from discord.ext import commands

from elbow_helper.configuration.channels import SUPPORT_TICKET_CATEGORY

from .config import SCAN_RETRY_COUNT, SCAN_RETRY_DELAY_SECONDS, STARTUP_SCAN_DELAY_SECONDS, TICKET_TYPES

LOGGER = logging.getLogger(__name__)


class RoutingMixin:
    def normalize_message(self, content: str) -> str:
        content = re.sub(r"<@!?\d+>", "", content)
        content = re.sub(r"<@&\d+>", "", content)
        content = re.sub(r"<#[0-9]+>", "", content)
        content = re.sub(r"\|\|.*?\|\|", "", content)
        return re.sub(r"\s+", " ", content).strip().lower()

    async def detect_ticket_type(self, channel: discord.TextChannel) -> tuple[str | None, str | None]:
        try:
            async for msg in channel.history(limit=10, oldest_first=True):
                norm = self.normalize_message(msg.content)
                if not norm:
                    continue
                for _, info in TICKET_TYPES.items():
                    if info["trigger"] in norm:
                        return info["emoji"], info["short"]
        except (discord.Forbidden, discord.HTTPException) as exc:
            LOGGER.warning("Failed to read history in #%s: %s", channel.name, exc)
        return None, None

    async def find_ticket_opener(self, channel: discord.TextChannel) -> discord.Member | None:
        try:
            async for msg in channel.history(limit=25, oldest_first=True):
                if msg.mentions:
                    member = channel.guild.get_member(msg.mentions[0].id)
                    if member:
                        return member
                if msg.author and not msg.author.bot:
                    return msg.author
        except (discord.Forbidden, discord.HTTPException) as exc:
            LOGGER.warning("Failed to resolve opener in #%s: %s", channel.name, exc)
        return None

    async def rename_ticket(self, channel: discord.TextChannel, emoji: str, short: str, opener: discord.abc.User):
        safe_user = opener.name if hasattr(opener, "name") else str(opener)
        username = re.sub(r"[^a-zA-Z0-9]", "-", safe_user).strip("-").lower() or "user"
        new_name = f"{emoji}｜{short}-{username}"
        if channel.name == new_name:
            return
        old_name = channel.name
        try:
            await channel.edit(name=new_name, reason="Rename support ticket")
        except discord.Forbidden:
            LOGGER.warning("Missing permissions to rename #%s", old_name)
        except discord.HTTPException as exc:
            LOGGER.warning("HTTP error renaming #%s: %s", old_name, exc)

    async def process_ticket_channel(self, channel: discord.TextChannel, *, explain: str):
        LOGGER.debug("Processing #%s (%s)", channel.name, explain)
        if channel.category_id != SUPPORT_TICKET_CATEGORY:
            return
        if not channel.name.startswith("ticket-"):
            return

        emoji, short = await self.detect_ticket_type(channel)
        if not emoji or not short:
            return

        opener = await self.find_ticket_opener(channel)
        if not opener:
            return

        await self.rename_ticket(channel, emoji, short, opener)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        if not isinstance(channel, discord.TextChannel):
            return
        if channel.category_id != SUPPORT_TICKET_CATEGORY:
            return
        if not channel.name.startswith("ticket-"):
            return

        for attempt in range(1, SCAN_RETRY_COUNT + 1):
            await asyncio.sleep(SCAN_RETRY_DELAY_SECONDS)
            await self.process_ticket_channel(channel, explain=f"create attempt {attempt}")
            if not channel.name.startswith("ticket-"):
                break

    async def scan_existing_tickets(self):
        await self.bot.wait_until_ready()
        try:
            for guild in self.bot.guilds:
                category = discord.utils.get(guild.categories, id=SUPPORT_TICKET_CATEGORY)
                if not category:
                    continue
                for channel in list(category.channels):
                    if isinstance(channel, discord.TextChannel) and channel.name.startswith("ticket-"):
                        await self.process_ticket_channel(channel, explain="startup scan")
                        await asyncio.sleep(STARTUP_SCAN_DELAY_SECONDS)
        except (discord.Forbidden, discord.HTTPException, RuntimeError):
            LOGGER.exception("Startup scan failed")
