"""Background loops for war polling and leadership-summary retention."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict

import discord

from .config import CLAN_TAGS, NOTICE_TTL

LOGGER = logging.getLogger(__name__)


class TaskMixin:

    async def _poll_coc_api(self):
        # Periodically poll the CoC API to drive war state updates
        await self.bot.wait_until_ready()
        # Prevent duplicate processing between startup replay and steady-state polling.
        await self._startup_sync_done.wait()
        while True:
            try:
                if not self.clash_client.configured:
                    await asyncio.sleep(60)
                    continue
                for clan in CLAN_TAGS:
                    data = await self._fetch_current_war(clan)
                    if data:
                        await self._sync_war_roles(clan, data)
                        await self._update_war_board(clan, data)
                        await self._handle_war_state(clan, data)
                    await asyncio.sleep(0.5)
                await asyncio.sleep(300)
            except asyncio.CancelledError:
                break
            except (asyncio.TimeoutError, discord.HTTPException, OSError, RuntimeError, TypeError, ValueError, KeyError) as e:
                LOGGER.exception("Error in CoC poll loop: %s", e)
                await asyncio.sleep(30)

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

    async def _sync_war_state_on_startup(self):
        """Restore transition state after restarts without replaying summaries."""
        await self.bot.wait_until_ready()
        try:
            if not self.clash_client.configured:
                return
            for clan in CLAN_TAGS:
                data = await self._fetch_current_war(clan)
                if not data:
                    continue
                await self._sync_war_roles(clan, data)
                state = (data.get("state") or "").lower()
                ctx = self.war_context.setdefault(clan, {"last_state": None, "processed_wars": set()})

                # An active war only needs its transition state restored.
                if state in {"preparation", "inwar"}:
                    ctx["last_state"] = state
                    self.war_context[clan] = ctx
                    await asyncio.sleep(0.5)
                    continue

                # Restore an ended war without replaying a processed summary.
                if state == "warended":
                    war_id = self._build_war_id(data)
                    processed = ctx.setdefault("processed_wars", set())
                    if war_id in processed or war_id in self.processed_war_ids:
                        ctx["last_state"] = state
                        self.war_context[clan] = ctx
                        await asyncio.sleep(0.5)
                        continue
                    # Not processed yet: defer to normal war-ended handler.
                    await self._handle_war_state(clan, data)
                    ctx["last_state"] = state
                    self.war_context[clan] = ctx
                    await asyncio.sleep(0.5)
        except (discord.Forbidden, discord.HTTPException, OSError, RuntimeError, TypeError, ValueError, KeyError) as e:
            LOGGER.exception("Startup war-state sync failed: %s", e)
        finally:
            self._startup_sync_done.set()
