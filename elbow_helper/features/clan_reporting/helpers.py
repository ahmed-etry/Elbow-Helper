"""Shared helper methods for clan data routing and date handling."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from elbow_helper.configuration.channels import CLAN_LEADERSHIP_CHANNELS

from .config import CLAN_DATA_THREADS
from .config import DISCORD_HTTP_MAX_RATE_LIMIT_RETRY_SECONDS
from .config import DISCORD_HTTP_RATE_LIMIT_RETRY_DELAYS_SECONDS


LOGGER = logging.getLogger(__name__)


class ClanReportingHelperMixin:
    """Thread, message, and date helpers used across clan-reporting flows."""

    def _leadership_channel_to_clan(self, channel_id: int) -> Optional[str]:
        for code, leadership_channel_id in CLAN_LEADERSHIP_CHANNELS.items():
            if leadership_channel_id == channel_id:
                return code
        return None

    async def _get_thread(self, clan_code: str) -> Optional[discord.abc.Messageable]:
        thread_id = CLAN_DATA_THREADS.get(clan_code)
        if not thread_id:
            return None

        channel = self.bot.get_channel(thread_id)
        if channel:
            return channel

        try:
            return await self.bot.fetch_channel(thread_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    async def _get_leadership_channel(self, clan_code: str) -> Optional[discord.TextChannel]:
        channel_id = CLAN_LEADERSHIP_CHANNELS.get(clan_code)
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        try:
            fetched = await self.bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return fetched if isinstance(fetched, discord.TextChannel) else None

    async def _fetch_message(
        self, channel: discord.abc.Messageable, message_id: int | str
    ) -> Optional[discord.Message]:
        try:
            parsed_message_id = int(message_id)
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                return await self._run_discord_http_operation(
                    "clan-reporting",
                    "fetch message",
                    lambda: channel.fetch_message(parsed_message_id),
                )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError, TypeError):
            return None
        return None

    @staticmethod
    def _is_rate_limited_discord_http_error(exc: discord.HTTPException) -> bool:
        return getattr(exc, "status", None) == 429

    @staticmethod
    def _discord_http_retry_seconds(exc: discord.HTTPException, attempt: int) -> float:
        retry_after = getattr(exc, "retry_after", None)
        if isinstance(retry_after, (int, float)) and retry_after > 0:
            return min(float(retry_after), DISCORD_HTTP_MAX_RATE_LIMIT_RETRY_SECONDS)
        return DISCORD_HTTP_RATE_LIMIT_RETRY_DELAYS_SECONDS[attempt]

    async def _run_discord_http_operation(
        self,
        context: str,
        operation: str,
        coro_factory,
    ):
        total_attempts = len(DISCORD_HTTP_RATE_LIMIT_RETRY_DELAYS_SECONDS) + 1
        for attempt in range(total_attempts):
            try:
                return await coro_factory()
            except (discord.NotFound, discord.Forbidden):
                raise
            except discord.HTTPException as exc:
                if not self._is_rate_limited_discord_http_error(exc):
                    raise
                if attempt >= len(DISCORD_HTTP_RATE_LIMIT_RETRY_DELAYS_SECONDS):
                    LOGGER.warning(
                        "Discord operation rate limited for %s during %s after %s attempts: %s",
                        context,
                        operation,
                        total_attempts,
                        exc,
                    )
                    return None
                delay = self._discord_http_retry_seconds(exc, attempt)
                LOGGER.warning(
                    "Discord operation rate limited for %s during %s; retrying in %.1fs",
                    context,
                    operation,
                    delay,
                )
                await asyncio.sleep(delay)
        return None

    def _previous_month_bounds(self, now: datetime) -> tuple[str, datetime, datetime]:
        year = now.year
        month = now.month

        if month == 1:
            prev_year = year - 1
            prev_month = 12
        else:
            prev_year = year
            prev_month = month - 1

        start = datetime(prev_year, prev_month, 1, 0, 0, tzinfo=timezone.utc)
        if prev_month == 12:
            end = datetime(prev_year + 1, 1, 1, 0, 0, tzinfo=timezone.utc)
        else:
            end = datetime(prev_year, prev_month + 1, 1, 0, 0, tzinfo=timezone.utc)

        return f"{prev_year}-{prev_month:02d}", start, end
