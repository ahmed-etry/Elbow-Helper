"""Auto responder and auto reaction listener."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass

import discord
from discord.ext import commands

from .config import AUTO_REACTION_EXCLUDED_EMOJIS
from .config import AUTO_REACTION_MAX_EMOJIS
from .config import AUTO_REACTION_SILENCE_WINDOW_SECONDS
from .config import KEYWORDS
from .config import KEYWORD_REPLIES
from .config import REACT_ALLOWED_CHANNEL_IDS
from .emoji_utils import extract_reaction_emojis
from .emoji_utils import filter_reaction_emojis
from .emoji_utils import prioritize_reaction_emojis

LOGGER = logging.getLogger(__name__)

ReactionBurstKey = tuple[int, int]


@dataclass
class AutoReactionBurst:
    last_seen_monotonic: float
    generation: int
    anchor_message: discord.Message
    emojis: list[str]
    task: asyncio.Task | None = None


class AutoTools(commands.Cog):
    """Auto responder and auto react utilities."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._reaction_bursts: dict[ReactionBurstKey, AutoReactionBurst] = {}
        self._reaction_lock = asyncio.Lock()
        self._reaction_tasks: set[asyncio.Task] = set()

    def cog_unload(self) -> None:
        for burst in tuple(self._reaction_bursts.values()):
            if burst.task and not burst.task.done():
                burst.task.cancel()
        for task in tuple(self._reaction_tasks):
            if not task.done():
                task.cancel()

    @staticmethod
    def _reaction_burst_key(message: discord.Message) -> ReactionBurstKey:
        return (message.channel.id, message.author.id)

    @staticmethod
    def _reaction_deadline(burst: AutoReactionBurst) -> float:
        return burst.last_seen_monotonic + AUTO_REACTION_SILENCE_WINDOW_SECONDS

    @staticmethod
    def _collect_message_emojis(message: discord.Message) -> list[str]:
        return filter_reaction_emojis(
            extract_reaction_emojis(message.content or ""),
            excluded=AUTO_REACTION_EXCLUDED_EMOJIS,
        )

    def _track_reaction_task(self, task: asyncio.Task) -> None:
        self._reaction_tasks.add(task)
        task.add_done_callback(self._reaction_tasks.discard)

    def _pop_expired_burst_locked(
        self,
        key: ReactionBurstKey,
        *,
        now_monotonic: float,
    ) -> AutoReactionBurst | None:
        burst = self._reaction_bursts.get(key)
        if burst is None or now_monotonic < self._reaction_deadline(burst):
            return None
        self._reaction_bursts.pop(key, None)
        if burst.task and not burst.task.done():
            burst.task.cancel()
        burst.task = None
        return burst

    def _schedule_burst_finalize_locked(self, key: ReactionBurstKey, burst: AutoReactionBurst) -> None:
        if burst.task and not burst.task.done():
            burst.task.cancel()
        deadline = self._reaction_deadline(burst)
        task = asyncio.create_task(self._finalize_burst_after_delay(key, burst.generation, deadline))
        burst.task = task
        self._track_reaction_task(task)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return

        react_allowed = (
            bool(REACT_ALLOWED_CHANNEL_IDS)
            and message.channel.id in REACT_ALLOWED_CHANNEL_IDS
        )
        if not react_allowed and not (message.content or ""):
            return

        content_lower = (message.content or "").lower()
        if any(word in content_lower for word in KEYWORDS):
            try:
                await message.reply(random.choice(KEYWORD_REPLIES))
            except (discord.Forbidden, discord.HTTPException) as exc:
                LOGGER.debug("Keyword reply failed: message_id=%s reason=%s", message.id, exc)
        if not react_allowed:
            return

        await self._queue_auto_reaction(message)

    async def _queue_auto_reaction(self, message: discord.Message) -> None:
        burst_key = self._reaction_burst_key(message)
        now_monotonic = time.monotonic()
        message_emojis = self._collect_message_emojis(message)
        expired_burst: AutoReactionBurst | None = None

        async with self._reaction_lock:
            expired_burst = self._pop_expired_burst_locked(burst_key, now_monotonic=now_monotonic)
            burst = self._reaction_bursts.get(burst_key)

            if burst is None:
                if not message_emojis:
                    burst = None
                else:
                    burst = AutoReactionBurst(
                        last_seen_monotonic=now_monotonic,
                        generation=1,
                        anchor_message=message,
                        emojis=list(message_emojis),
                    )
                    self._reaction_bursts[burst_key] = burst
            else:
                burst.last_seen_monotonic = now_monotonic
                burst.anchor_message = message
                for emoji_token in message_emojis:
                    if emoji_token not in burst.emojis:
                        burst.emojis.append(emoji_token)
                burst.generation += 1

            if burst is not None:
                self._schedule_burst_finalize_locked(burst_key, burst)

        if expired_burst is not None:
            await self._emit_burst_reactions(expired_burst)

    async def _finalize_burst_after_delay(
        self,
        key: ReactionBurstKey,
        generation: int,
        deadline: float,
    ) -> None:
        while True:
            try:
                await asyncio.sleep(max(0.0, deadline - time.monotonic()))
            except asyncio.CancelledError:
                return

            async with self._reaction_lock:
                burst = self._reaction_bursts.get(key)
                if burst is None or burst.generation != generation:
                    return

                deadline = self._reaction_deadline(burst)
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    continue

                self._reaction_bursts.pop(key, None)
                if burst.task is asyncio.current_task():
                    burst.task = None
                break

        await self._emit_burst_reactions(burst)

    async def _emit_burst_reactions(self, burst: AutoReactionBurst) -> None:
        selected_emojis = prioritize_reaction_emojis(
            burst.emojis,
            limit=AUTO_REACTION_MAX_EMOJIS,
        )
        if not selected_emojis:
            return

        for emoji_token in selected_emojis:
            try:
                await burst.anchor_message.add_reaction(emoji_token)
            except discord.NotFound:
                LOGGER.debug("Auto reaction anchor missing: message_id=%s", burst.anchor_message.id)
                return
            except discord.Forbidden as exc:
                LOGGER.debug(
                    "Auto reaction forbidden: message_id=%s reason=%s",
                    burst.anchor_message.id,
                    exc,
                )
                return
            except discord.HTTPException as exc:
                LOGGER.debug(
                    "Auto reaction failed: message_id=%s emoji=%s reason=%s",
                    burst.anchor_message.id,
                    emoji_token,
                    exc,
                )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AutoTools(bot))
