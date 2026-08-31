"""War state machine transitions and summary posting."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import discord
from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.clans import CLAN_CODES_BY_NAME

from .rendering import build_war_summary_embed

LOGGER = logging.getLogger(__name__)


class WarflowMixin:

    async def _find_existing_war_summary(
        self,
        channel: discord.abc.Messageable,
        expected_embed: discord.Embed,
        ended_at: datetime,
    ) -> discord.Message | None:
        bot_user_id = getattr(getattr(self.bot, "user", None), "id", None)
        if bot_user_id is None:
            raise RuntimeError("Cannot reconcile war summaries before the bot user is available")
        async for message in channel.history(
            limit=None,
            after=ended_at - timedelta(minutes=5),
            oldest_first=True,
        ):
            if message.author.id != bot_user_id:
                continue
            for embed in message.embeds:
                if (
                    embed.title != expected_embed.title
                    or embed.description != expected_embed.description
                ):
                    continue
                if embed.timestamp is None:
                    continue
                if int(embed.timestamp.timestamp()) == int(ended_at.timestamp()):
                    return message
        return None

    async def _persist_war_summary_completion(
        self,
        war_id: str,
        processed: set[str],
        message: discord.Message | None,
    ) -> None:
        async with self._war_summary_state_lock:
            previous_order = list(self.processed_war_order)
            previous_ids = set(self.processed_war_ids)
            previous_processed_cache = self.cache.get("processed_wars")
            message_key = str(message.id) if message is not None else None
            previous_summary = (
                self.summary_registry.get(message_key)
                if message_key is not None
                else None
            )

            if message is not None and message_key is not None:
                self.summary_registry[message_key] = {
                    "channel": message.channel.id,
                    "sent_at": int(message.created_at.timestamp()),
                }
                self.cache["summary_messages"] = self.summary_registry
            processed.add(war_id)
            self._record_processed_war(war_id)
            try:
                await self._save_cache_async()
            except (OSError, TypeError):
                processed.discard(war_id)
                self.processed_war_order = previous_order
                self.processed_war_ids = previous_ids
                if previous_processed_cache is None:
                    self.cache.pop("processed_wars", None)
                else:
                    self.cache["processed_wars"] = previous_processed_cache
                if message_key is not None:
                    if previous_summary is None:
                        self.summary_registry.pop(message_key, None)
                    else:
                        self.summary_registry[message_key] = previous_summary
                    if self.summary_registry:
                        self.cache["summary_messages"] = self.summary_registry
                    else:
                        self.cache.pop("summary_messages", None)
                raise

    async def _handle_war_state(self, clan: str, data: Dict[str, Any]):
        # Main state machine driven by the CoC API
        state = (data.get("state") or "").lower()
        ctx = self.war_context.setdefault(clan, {"last_state": None, "processed_wars": set()})
        last_state = ctx.get("last_state")
        channel_meta = self.clan_channels.get(clan, {})
        leadership_channel_id = channel_meta.get("leadership_channel")
        leadership_channel = self.bot.get_channel(leadership_channel_id) if leadership_channel_id else None
        leadership_role = channel_meta.get("leadership_role")
        clan_code = CLAN_CODES_BY_NAME.get(clan)
        clan_config = CLANS.get(clan_code) if clan_code else None
        is_utility_clan = bool(clan_config and clan_config.is_utility)

        # Track active-war transitions without posting another leadership summary.
        if state in {"preparation", "inwar"} and last_state not in {"preparation", "inwar"}:
            ctx["last_state"] = state
            return

        # War ended: send summary once per war
        if state == "warended":
            war_id = self._build_war_id(data)
            lock = self._war_state_locks.setdefault(clan, asyncio.Lock())
            async with lock:
                processed = ctx.setdefault("processed_wars", set())
                in_flight = self._wars_in_flight.setdefault(clan, set())
                # Lock serializes per-clan handlers; in-flight guards duplicate work within one lock window.
                if war_id in processed or war_id in self.processed_war_ids or war_id in in_flight:
                    ctx["last_state"] = state
                    return
                in_flight.add(war_id)
                try:
                    missed = self._compute_missed(data)
                    end_time = self._coctime_to_dt(data.get("endTime"))
                    prep_start = self._coctime_to_dt(data.get("preparationStartTime"))
                    if prep_start is None:
                        start_time = self._coctime_to_dt(data.get("startTime"))
                        if start_time is not None:
                            prep_start = start_time - timedelta(hours=23)

                    ended_at = end_time or datetime.now(timezone.utc)
                    mention_txt = f"<@&{leadership_role}>" if leadership_role else ""
                    cwl_note = self._cwl_note_for_end(ended_at, prep_start=prep_start)

                    summary_embed = build_war_summary_embed(
                        data,
                        await self.war_emojis.get(),
                        timestamp=ended_at,
                    )
                    self._add_missed_attack_fields(summary_embed, missed)

                    msg: discord.Message | None = None
                    if leadership_channel:
                        note_line = f"{mention_txt} {cwl_note}".strip()
                        msg = await self._find_existing_war_summary(
                            leadership_channel,
                            summary_embed,
                            ended_at,
                        )
                        if msg is None:
                            try:
                                msg = await leadership_channel.send(
                                    content=note_line or None,
                                    embed=summary_embed,
                                )
                            except discord.Forbidden as e:
                                LOGGER.warning(
                                    "Failed to send war summary for %s (%s): %s. Marking as processed to avoid retries.",
                                    clan,
                                    war_id,
                                    e,
                                )
                                msg = None
                            except discord.HTTPException as e:
                                LOGGER.warning(
                                    "War summary delivery for %s (%s) is unresolved and will be reconciled: %s",
                                    clan,
                                    war_id,
                                    e,
                                )
                                raise
                    elif leadership_channel_id:
                        LOGGER.warning(
                            "Leadership channel %s for %s was not resolved; marking war %s processed without summary post.",
                            leadership_channel_id,
                            clan,
                            war_id,
                        )
                    elif is_utility_clan:
                        LOGGER.info(
                            "Skipping war summary post for utility clan %s; war %s has no leadership channel configured.",
                            clan,
                            war_id,
                        )
                    else:
                        LOGGER.warning(
                            "No leadership channel configured for %s; marking war %s processed without summary post.",
                            clan,
                            war_id,
                        )
                    await self._persist_war_summary_completion(
                        war_id,
                        processed,
                        msg,
                    )
                    ctx["last_state"] = state
                    self.war_context[clan] = ctx
                    if leadership_channel and msg is not None:
                        await self._cleanup_previous_summary_messages(
                            leadership_channel,
                            keep_message_id=msg.id,
                        )
                    return
                finally:
                    in_flight.discard(war_id)

        ctx["last_state"] = state
