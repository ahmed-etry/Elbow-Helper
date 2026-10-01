from __future__ import annotations

import asyncio
import logging
import re

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

    def public_news_prompt_source(self, prompt: discord.Message) -> int | None:
        """Identify one live publication prompt from this feature."""
        source_id = getattr(getattr(prompt, "reference", None), "message_id", None)
        if (getattr(getattr(prompt, "author", None), "id", None)
                != getattr(getattr(self.bot, "user", None), "id", None)
                or prompt.content != f"Publish this update to <#{PUBLIC_NEWS}>?"
                or not source_id):
            return None
        return int(source_id)

    async def dismiss_public_news_prompt(self, prompt: discord.Message) -> bool:
        """Remove a publication prompt for its button and the agent."""
        if self.public_news_prompt_source(prompt) is None:
            return False
        try:
            await prompt.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False
        return True

    async def find_public_news_prompts(self, source_message: discord.Message) -> tuple[discord.Message, ...]:
        """Find prompts that still offer to publish this update."""
        matches = []
        async for message in source_message.channel.history(after=source_message, limit=None):
            if self.public_news_prompt_source(message) == source_message.id:
                matches.append(message)
        return tuple(matches)

    @staticmethod
    def public_news_preview(source_message: discord.Message) -> dict[str, object]:
        """Describe the exact text and attachments forwarded from a lead update."""
        content = re.sub(r"<@&\d+>", "", source_message.content or "").strip()
        attachments = tuple(source_message.attachments[:3])
        return {"content": content, "attachments": attachments}

    async def publish_public_news(self, source_message: discord.Message,
                                  target_channel: discord.TextChannel,
                                  *, prepared: dict[str, object] | None = None,
                                  prompts: tuple[discord.Message, ...] = ()) -> discord.Message:
        """Forward one update for its panel button and the agent."""
        prepared = prepared or self.public_news_preview(source_message)
        files = []
        for attachment in prepared["attachments"]:
            try:
                files.append(await attachment.to_file())
            except discord.HTTPException:
                LOGGER.debug("Failed converting attachment %s", attachment.id)
        sent = await target_channel.send(
            content=prepared["content"] or None,
            files=files or None,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True,
            ),
        )
        for prompt in prompts:
            await self.dismiss_public_news_prompt(prompt)
        return sent

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
