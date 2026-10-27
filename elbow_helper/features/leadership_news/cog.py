from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import re
import time

import discord
from discord.ext import commands

from elbow_helper.configuration.channels import LEAD_NEWS, PUBLIC_NEWS
from elbow_helper.infrastructure.persistence import read_json, write_json_atomic

from .views import ForwardView

LOGGER = logging.getLogger(__name__)
STATE_FILE = Path("data/leadership_news/state.json")


class LeadNews(commands.Cog):
    """Leadership news helper: propose forwarding leadership updates to public news."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        try:
            self._state = read_json(STATE_FILE)
        except FileNotFoundError:
            self._state = {"published": {}, "prompts": {}}
        self._publish_locks: dict[int, asyncio.Lock] = {}
        self._cleanup_tasks: dict[int, asyncio.Task] = {}
        self._prompt_views: dict[int, ForwardView] = {}

    async def cog_load(self) -> None:
        for prompt_id in self._state["prompts"]:
            self._register_prompt(int(prompt_id))

    def cog_unload(self):
        for task in self._cleanup_tasks.values():
            if not task.done():
                task.cancel()
        for view in self._prompt_views.values():
            view.stop()

    def _register_prompt(self, prompt_id: int, view: ForwardView | None = None) -> None:
        pending = self._state["prompts"][str(prompt_id)]
        view = view or ForwardView(
            self.bot, pending["source_message_id"], prompt_id, pending["channel_id"],
            timeout=None,
        )
        self.bot.add_view(view, message_id=prompt_id)
        self._prompt_views[prompt_id] = view
        task = asyncio.create_task(self._cleanup_prompt(prompt_id))
        self._cleanup_tasks[prompt_id] = task

    def _forget_prompt(self, prompt_id: int) -> None:
        if self._state["prompts"].pop(str(prompt_id), None) is not None:
            write_json_atomic(STATE_FILE, self._state)
        view = self._prompt_views.pop(prompt_id, None)
        if view is not None:
            view.stop()
        task = self._cleanup_tasks.pop(prompt_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _cleanup_prompt(self, prompt_id: int) -> None:
        pending = self._state["prompts"][str(prompt_id)]
        await self.bot.wait_until_ready()
        await asyncio.sleep(max(0, pending["expires_at"] - time.time()))
        while str(prompt_id) in self._state["prompts"]:
            try:
                channel = self.bot.get_channel(pending["channel_id"])
                if channel is None:
                    channel = await self.bot.fetch_channel(pending["channel_id"])
                await channel.get_partial_message(prompt_id).delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                LOGGER.debug("Failed auto-deleting prompt %s", prompt_id)
                await asyncio.sleep(60)
                continue
            self._forget_prompt(prompt_id)

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
        except discord.NotFound:
            pass
        except discord.HTTPException:
            return False
        self._forget_prompt(prompt.id)
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
                                  prompts: tuple[discord.Message, ...] = ()) -> discord.Message | discord.PartialMessage:
        """Forward one update for its panel button and the agent."""
        lock = self._publish_locks.setdefault(source_message.id, asyncio.Lock())
        async with lock:
            published = self._state["published"].get(str(source_message.id))
            if published is not None:
                channel = self.bot.get_partial_messageable(
                    published["channel_id"], guild_id=published["guild_id"],
                )
                sent = channel.get_partial_message(published["message_id"])
            else:
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
                self._state["published"][str(source_message.id)] = {
                    "channel_id": target_channel.id, "message_id": sent.id,
                    "guild_id": target_channel.guild.id,
                }
            # Persist the receipt before any prompt cleanup can fail or be cancelled.
            write_json_atomic(STATE_FILE, self._state)
            for prompt in prompts:
                await self.dismiss_public_news_prompt(prompt)
            return sent

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if LEAD_NEWS == 0 or message.channel.id != LEAD_NEWS:
            return
        if str(message.id) in self._state["published"]:
            return

        view = ForwardView(
            self.bot, message.id, prompt_message_id=0,
            prompt_channel_id=message.channel.id, timeout=None,
        )
        try:
            prompt = await message.reply(
                content=f"Publish this update to <#{PUBLIC_NEWS}>?",
                mention_author=False, view=view,
            )
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.exception("Failed creating publish prompt for message %s", message.id)
            return

        view.prompt_message_id = prompt.id
        self._state["prompts"][str(prompt.id)] = {
            "source_message_id": message.id,
            "channel_id": prompt.channel.id,
            "expires_at": time.time() + 86400,
        }
        write_json_atomic(STATE_FILE, self._state)
        self._register_prompt(prompt.id, view)
