from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from elbow_helper.configuration.channels import LEAD_NEWS, PUBLIC_NEWS

from .views import ForwardView

LOGGER = logging.getLogger(__name__)


class LeadNews(commands.Cog):
    """Leadership news helper: propose forwarding leadership updates to public news."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cleanup_tasks: set[asyncio.Task] = set()

    def cog_unload(self):
        for task in self._cleanup_tasks:
            if not task.done():
                task.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if LEAD_NEWS == 0 or message.channel.id != LEAD_NEWS:
            return

        try:
            prompt = await message.reply(content=f"Publish this update to <#{PUBLIC_NEWS}>?", mention_author=False)
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.exception("Failed creating publish prompt for message %s", message.id)
            return

        view = ForwardView(
            self.bot,
            message.id,
            prompt_message_id=prompt.id,
            prompt_channel_id=prompt.channel.id,
            timeout=None,
        )
        try:
            await prompt.edit(view=view)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LOGGER.exception("Failed attaching publish view for prompt %s", prompt.id)
            return

        async def delete_later():
            try:
                await asyncio.sleep(86400)
                await prompt.delete()
            except (asyncio.CancelledError, discord.NotFound):
                return
            except (discord.Forbidden, discord.HTTPException):
                LOGGER.debug("Failed auto-deleting prompt %s", prompt.id)

        task = asyncio.create_task(delete_later())
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)
