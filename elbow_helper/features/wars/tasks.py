"""Background loop for leadership-summary retention."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict

import discord

from .config import NOTICE_TTL

LOGGER = logging.getLogger(__name__)


class TaskMixin:

    async def _periodic_summary_cleanup(self):
        # Sweep leadership channels hourly and delete final summaries after 48h.
        await self.bot.wait_until_ready()
        await self._startup_sync_done.wait()
        while True:
            try:
                await self._cleanup_summary_messages()
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                break
            except (discord.Forbidden, discord.HTTPException, OSError, RuntimeError, TypeError, ValueError) as e:
                LOGGER.exception("Error in periodic summary cleanup: %s", e)
                await asyncio.sleep(60)

    async def _cleanup_summary_messages(self):
        # Remove stale war summary embeds from leadership channels
        if not self.summary_registry:
            return
        now_ts = int(datetime.now(timezone.utc).timestamp())
        keep: Dict[str, Dict[str, int]] = {}
        for msg_id_str, entry in self.summary_registry.items():
            chan_id = entry.get("channel")
            sent_at = entry.get("sent_at")
            if not chan_id or not sent_at:
                continue
            if now_ts - sent_at < NOTICE_TTL:
                keep[msg_id_str] = entry
                continue

            channel = self.bot.get_channel(chan_id)
            if not channel:
                try:
                    channel = await self.bot.fetch_channel(chan_id)
                except discord.NotFound:
                    continue
                except discord.Forbidden:
                    keep[msg_id_str] = entry
                    continue
                except discord.HTTPException:
                    keep[msg_id_str] = entry
                    continue
            if not hasattr(channel, "fetch_message"):
                continue
            try:
                msg = await channel.fetch_message(int(msg_id_str))
                await msg.delete()
            except discord.NotFound:
                pass
            except discord.Forbidden:
                keep[msg_id_str] = entry
            except discord.HTTPException:
                keep[msg_id_str] = entry

        if keep != self.summary_registry:
            self.summary_registry = keep
            self.cache["summary_messages"] = keep
            await self._save_cache_async()

    async def _cleanup_previous_summary_messages(
        self,
        channel: discord.abc.Messageable,
        *,
        keep_message_id: int,
    ) -> None:
        """Remove older registered summaries after their replacement is posted."""
        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            return

        keep: Dict[str, Dict[str, int]] = {}
        for msg_id_str, entry in self.summary_registry.items():
            if entry.get("channel") != channel_id or msg_id_str == str(keep_message_id):
                keep[msg_id_str] = entry
                continue
            try:
                message = await channel.fetch_message(int(msg_id_str))
                await message.delete()
            except discord.NotFound:
                continue
            except (discord.Forbidden, discord.HTTPException):
                keep[msg_id_str] = entry

        if keep != self.summary_registry:
            self.summary_registry = keep
            self.cache["summary_messages"] = keep
            await self._save_cache_async()
