from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import discord

from elbow_helper.configuration.channels import TICKETS_LOG

from .config import (
    MAX_TICKET_LINKS_IN_LEAVE_EMBED,
    TICKET_LOG_SCAN_BATCH_DELAY_SECONDS,
    TICKET_LOG_SCAN_BATCH_SIZE,
    TICKET_LOG_SCAN_MAX_RETRY_SECONDS,
    TICKET_LOG_SCAN_RETRY_SECONDS,
)
from .state import save_state

LOGGER = logging.getLogger(__name__)


class TicketIndexMixin:
    @staticmethod
    def _ticket_owner_ids_in_message(message: discord.Message) -> set[str]:
        owner_ids: set[str] = set()
        for embed in message.embeds:
            for field in embed.fields:
                if (field.name or "").strip().lower() != "ticket owner":
                    continue
                owner_ids.update(re.findall(r"\d{15,22}", field.value or ""))
        return owner_ids

    def _index_ticket_log_message(self, message: discord.Message) -> bool:
        changed = False
        owner_ids = self._ticket_owner_ids_in_message(message)
        if owner_ids:
            owner_links = self.state["ticket_owner_links"]
            for owner_id in owner_ids:
                links = owner_links.setdefault(owner_id, [])
                if message.jump_url not in links:
                    links.append(message.jump_url)
                    changed = True
        last_message_id_raw = self.state.get("ticket_log_last_message_id")
        try:
            last_message_id = int(last_message_id_raw) if last_message_id_raw else 0
        except (TypeError, ValueError):
            last_message_id = 0
        if message.id > last_message_id:
            self.state["ticket_log_last_message_id"] = str(message.id)
            changed = True
        return changed

    @staticmethod
    def _ticket_scan_retry_seconds(error: discord.HTTPException) -> float:
        retry_after = getattr(error, "retry_after", None)
        if isinstance(retry_after, (int, float)) and retry_after > 0:
            return min(float(retry_after), TICKET_LOG_SCAN_MAX_RETRY_SECONDS)
        return TICKET_LOG_SCAN_RETRY_SECONDS

    async def _refresh_ticket_log_index(self, guild: discord.Guild) -> None:
        channel = guild.get_channel(TICKETS_LOG)
        if channel is None:
            try:
                channel = await guild.fetch_channel(TICKETS_LOG)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                LOGGER.debug("Ticket log channel not found: %s", TICKETS_LOG)
                return
        if not isinstance(channel, discord.TextChannel):
            LOGGER.debug("Ticket log channel is not a text channel: %s", TICKETS_LOG)
            return

        async with self._ticket_index_lock:
            last_id_raw = self.state.get("ticket_log_last_message_id")
            try:
                cursor_id = int(last_id_raw) if last_id_raw else None
            except (TypeError, ValueError):
                cursor_id = None

            while True:
                history_kwargs: dict[str, Any] = {
                    "limit": TICKET_LOG_SCAN_BATCH_SIZE,
                    "oldest_first": True,
                }
                if cursor_id is not None:
                    history_kwargs["after"] = discord.Object(id=cursor_id)
                try:
                    batch = [msg async for msg in channel.history(**history_kwargs)]
                except discord.Forbidden:
                    LOGGER.warning("Missing permissions to scan ticket logs")
                    return
                except discord.HTTPException as exc:
                    if exc.status == 429:
                        wait_for = self._ticket_scan_retry_seconds(exc)
                        LOGGER.warning("Ticket-log scan hit rate limit; retrying in %.1fs", wait_for)
                        await asyncio.sleep(wait_for)
                        continue
                    LOGGER.warning("Ticket-log scan failed: %s", exc)
                    return

                if not batch:
                    self.state["ticket_log_index_ready"] = True
                    save_state(self.state)
                    return

                for message in batch:
                    self._index_ticket_log_message(message)
                cursor_id = batch[-1].id
                self.state["ticket_log_last_message_id"] = str(cursor_id)
                save_state(self.state)

                if len(batch) < TICKET_LOG_SCAN_BATCH_SIZE:
                    self.state["ticket_log_index_ready"] = True
                    save_state(self.state)
                    return
                await asyncio.sleep(TICKET_LOG_SCAN_BATCH_DELAY_SECONDS)

    async def _find_ticket_log_links_for_member(
        self,
        guild: discord.Guild,
        member_id: int,
        max_links: int = MAX_TICKET_LINKS_IN_LEAVE_EMBED,
    ) -> tuple[list[str], int]:
        await self._refresh_ticket_log_index(guild)
        links = self.state.get("ticket_owner_links", {}).get(str(member_id), [])
        if not links:
            return [], 0
        ordered = list(reversed(links))
        return ordered[:max_links], len(ordered)
