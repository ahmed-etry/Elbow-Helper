"""War state machine transitions and summary posting."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import discord
from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.clans import CLAN_CODES_BY_NAME
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX
from elbow_helper.configuration.style import DEFAULT_THUMBNAIL_URL

LOGGER = logging.getLogger(__name__)


class WarflowMixin:

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
                    opponent_name = (data.get("opponent") or {}).get("name") or "Unknown"
                    clan_score = (data.get("clan") or {}).get("stars")
                    opp_score = (data.get("opponent") or {}).get("stars")
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

                    summary_embed = discord.Embed(
                        title="\u231b Clan War Ended",
                        description=f"War against _{opponent_name}_ is over.",
                        color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
                        timestamp=ended_at,
                    )
                    summary_embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
                    if clan_score is not None and opp_score is not None:
                        summary_embed.add_field(name="\u2b50 Score", value=f"{clan_score} - {opp_score}", inline=True)
                    self._add_missed_attack_fields(summary_embed, missed)
                    summary_embed.add_field(
                        name="\U0001f5d3\ufe0f War Ended",
                        value=f"<t:{int(ended_at.timestamp())}:F> (<t:{int(ended_at.timestamp())}:R>)",
                        inline=False,
                    )

                    if leadership_channel:
                        note_line = f"{mention_txt} {cwl_note}".strip()
                        try:
                            msg = await leadership_channel.send(content=note_line or None, embed=summary_embed)
                        except (discord.Forbidden, discord.HTTPException) as e:
                            LOGGER.warning(
                                "Failed to send war summary for %s (%s): %s. Marking as processed to avoid retries.",
                                clan,
                                war_id,
                                e,
                            )
                        else:
                            sent_at = int(msg.created_at.replace(tzinfo=timezone.utc).timestamp())
                            self.summary_registry[str(msg.id)] = {
                                "channel": msg.channel.id,
                                "sent_at": sent_at,
                            }
                            self.cache["summary_messages"] = self.summary_registry
                            await self._cleanup_previous_summary_messages(
                                msg.channel,
                                keep_message_id=msg.id,
                            )
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

                    processed.add(war_id)
                    self._record_processed_war(war_id)
                    await self._save_cache_async()
                    ctx["last_state"] = state
                    self.war_context[clan] = ctx
                    return
                finally:
                    in_flight.discard(war_id)

        ctx["last_state"] = state
