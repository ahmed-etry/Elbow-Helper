"""Voice-channel lifecycle and rendering for event stats."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from datetime import timedelta
from typing import Any

import discord

from elbow_helper.infrastructure.time import utc_now

from .config import COUNTDOWN_MINUTE_INCREMENT
from .config import DEFAULT_GRACE_HOURS
from .config import ENDING_SOON_HOURS
from .config import HIGH_PRECISION_THRESHOLD_HOURS
from .config import MAX_CHANNEL_NAME_LENGTH
from .state import save_state
from .timeutils import fmt_dh

LOGGER = logging.getLogger(__name__)


class EventStatsChannelsMixin:
    @staticmethod
    def _is_channel_edit_rate_limited(exc: discord.HTTPException) -> bool:
        status = getattr(exc, "status", None)
        code = getattr(exc, "code", None)
        text = str(exc).lower()
        return (
            status == 429
            or code == 40062
            or "too many requests" in text
            or "service resource is being rate limited" in text
        )

    @staticmethod
    def _countdown_text(delta: timedelta) -> str:
        return fmt_dh(delta, minute_step=COUNTDOWN_MINUTE_INCREMENT)

    def _recurring_schedule_shape(self, event: dict[str, Any]) -> str:
        return str(event.get("schedule_shape") or "range")

    def _recurring_range(self, event: dict[str, Any], reference: datetime) -> tuple[datetime, datetime]:
        return event["range_fn"](reference)

    def _recurring_point(self, event: dict[str, Any], reference: datetime) -> datetime:
        return event["point_fn"](reference)

    def _compose_channel_name(self, event_name: str, suffix: str) -> str:
        suffix_text = f": {suffix}"
        max_base_length = MAX_CHANNEL_NAME_LENGTH - len(suffix_text)
        base = (event_name or "Event").strip() or "Event"
        if len(base) > max_base_length:
            if max_base_length > 3:
                base = f"{base[: max_base_length - 3].rstrip()}..."
            else:
                base = base[:max_base_length]
        return f"{base}{suffix_text}"

    def _counter_value(self, guild: discord.Guild | None, event: dict[str, Any]) -> int | None:
        if guild is None:
            return None
        roles = []
        for role_id in event.get("roles_to_count") or []:
            role = guild.get_role(role_id)
            if role is not None:
                roles.append(role)
        member_ids = {member.id for role in roles for member in role.members}
        return len(member_ids)

    def _uses_high_precision_countdown(self, event: dict[str, Any], now: datetime) -> bool:
        if not event.get("enabled", True):
            return False

        threshold = timedelta(hours=HIGH_PRECISION_THRESHOLD_HOURS)

        if event["type"] == "one-time":
            start = event["start"]
            end = event["end"]
            if now < start:
                return (start - now) < threshold
            if start <= now < end:
                return (end - now) < threshold
            return False

        if event["type"] == "recurring":
            if self._recurring_schedule_shape(event) == "point":
                return (self._recurring_point(event, now) - now) < threshold

            start, end = self._recurring_range(event, now)
            if now < start:
                return (start - now) < threshold
            if start <= now < end:
                return (end - now) < threshold
            return False

        return False

    def _requires_high_precision_refresh(self, now: datetime) -> bool:
        return any(self._uses_high_precision_countdown(event, now) for event in self.events)

    def _one_time_suffix(self, event: dict[str, Any], now: datetime) -> tuple[str, bool]:
        start = event["start"]
        end = event["end"]
        grace = timedelta(hours=int(event.get("grace_period_hours", DEFAULT_GRACE_HOURS)))

        if now < start:
            return self._countdown_text(start - now), False
        if start <= now < end:
            remaining = end - now
            if remaining <= timedelta(hours=ENDING_SOON_HOURS):
                return f"Ends Soon {self._countdown_text(remaining)}", False
            return "Live!", False
        if end <= now < end + grace:
            return "Ended", False
        return "Ended", True

    def _recurring_suffix(self, event: dict[str, Any], now: datetime) -> str:
        if self._recurring_schedule_shape(event) == "point":
            next_point = self._recurring_point(event, now)
            remaining = next_point - now
            if remaining <= timedelta(seconds=0):
                return "Now!"
            if remaining <= timedelta(hours=ENDING_SOON_HOURS):
                return f"Soon {self._countdown_text(remaining)}"
            return self._countdown_text(remaining)

        start, end = self._recurring_range(event, now)
        grace = timedelta(hours=int(event.get("grace_period_hours", DEFAULT_GRACE_HOURS)))

        if now < start:
            return self._countdown_text(start - now)
        if start <= now < end:
            remaining = end - now
            if remaining <= timedelta(hours=ENDING_SOON_HOURS):
                return f"Ends Soon {self._countdown_text(remaining)}"
            return "Live!"
        if end <= now < end + grace:
            return "Ended"

        future_ref = now + timedelta(hours=1)
        next_start, _ = self._recurring_range(event, future_ref)
        if next_start <= now:
            next_start, _ = self._recurring_range(event, now + timedelta(days=1))
        return self._countdown_text(next_start - now)

    def _event_phase(self, event: dict[str, Any], now: datetime) -> str:
        if not event.get("enabled", True):
            return "disabled"

        if event["type"] == "counter":
            return "live"

        if event["type"] == "one-time":
            start = event["start"]
            end = event["end"]
            grace = timedelta(hours=int(event.get("grace_period_hours", DEFAULT_GRACE_HOURS)))
            if now < start:
                return "upcoming"
            if start <= now < end:
                return "live"
            if end <= now < end + grace:
                return "ended"
            return "expired"

        if self._recurring_schedule_shape(event) == "point":
            return "upcoming"

        start, end = self._recurring_range(event, now)
        grace = timedelta(hours=int(event.get("grace_period_hours", DEFAULT_GRACE_HOURS)))
        if now < start:
            return "upcoming"
        if start <= now < end:
            return "live"
        if end <= now < end + grace:
            return "ended"
        return "upcoming"

    def describe_event_status(self, event: dict[str, Any], guild: discord.Guild | None = None) -> str:
        if not event.get("enabled", True):
            return "Disabled"

        now = utc_now()
        if event["type"] == "counter":
            count = self._counter_value(guild, event)
            if count is None:
                return "Member count unavailable"
            return "1 member" if count == 1 else f"{count} members"
        if event["type"] == "recurring":
            return self._recurring_suffix(event, now)
        suffix, delete_after_grace = self._one_time_suffix(event, now)
        return "Expired" if delete_after_grace else suffix

    def _sort_key_for_event(self, event: dict[str, Any], now: datetime) -> tuple[int, int, float, str]:
        position = int(event.get("position", 0))
        name = str(event.get("name") or "")
        phase = self._event_phase(event, now)

        if event["type"] == "counter":
            return (0, position, 0.0, name)

        if event["type"] == "one-time":
            start = event["start"]
            if phase == "live":
                return (1, position, start.timestamp(), name)
            if phase == "upcoming":
                return (2, position, start.timestamp(), name)
            return (3, position, start.timestamp(), name)

        if self._recurring_schedule_shape(event) == "point":
            next_point = self._recurring_point(event, now)
            return (2, position, next_point.timestamp(), name)

        start, _ = self._recurring_range(event, now)
        if phase == "live":
            return (1, position, start.timestamp(), name)
        if phase == "upcoming":
            return (2, position, start.timestamp(), name)
        future_ref = now + timedelta(hours=1)
        next_start, _ = self._recurring_range(event, future_ref)
        if next_start <= now:
            next_start, _ = self._recurring_range(event, now + timedelta(days=1))
        return (3, position, next_start.timestamp(), name)

    def _resolve_category(self, guild: discord.Guild, event: dict[str, Any]) -> discord.CategoryChannel | None:
        category_id = event.get("category_id")
        if not isinstance(category_id, int):
            return None
        category = guild.get_channel(category_id)
        if isinstance(category, discord.CategoryChannel):
            return category

        state_event = self._find_state_event(event["key"])
        if state_event is not None:
            state_event["category_id"] = None
            event["category_id"] = None
            save_state(self.state)
        LOGGER.warning("Configured category no longer exists for event %s", event["key"])
        return None

    def _set_channel_id(self, event_key: str, channel_id: int | None) -> None:
        state_event = self._find_state_event(event_key)
        if state_event is not None:
            state_event["channel_id"] = channel_id
        event = self.get_event(event_key)
        if event is not None:
            event["channel_id"] = channel_id
        save_state(self.state)

    async def _delete_event_channel(
        self,
        guild: discord.Guild,
        event: dict[str, Any],
        *,
        reason: str,
        clear_state: bool = True,
    ) -> bool:
        channel_id = event.get("channel_id")
        channel = guild.get_channel(channel_id) if isinstance(channel_id, int) else None
        if isinstance(channel, discord.VoiceChannel):
            try:
                await channel.delete(reason=reason)
            except discord.Forbidden:
                LOGGER.warning("Missing permissions deleting event channel %s", channel.id)
                return False
            except discord.HTTPException as exc:
                LOGGER.warning("HTTP error deleting event channel %s: %s", channel.id, exc)
                return False

        if clear_state and channel_id is not None:
            self._set_channel_id(event["key"], None)
        return True

    async def _prune_one_time_event(self, guild: discord.Guild, event: dict[str, Any]) -> bool:
        deleted = await self._delete_event_channel(
            guild,
            event,
            reason="One-time event expired and was removed",
            clear_state=False,
        )
        if not deleted and event.get("channel_id"):
            return False
        self.state["events"] = [item for item in self._state_events() if item.get("key") != event["key"]]
        self._persist_state()
        LOGGER.info("Pruned one-time event %s", event["name"])
        return True

    async def _ensure_channels(self, guild: discord.Guild) -> bool:
        everyone = guild.default_role
        bot_member = guild.me
        if bot_member is None and self.bot.user:
            try:
                bot_member = await guild.fetch_member(self.bot.user.id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                bot_member = None

        overwrites = {everyone: discord.PermissionOverwrite(view_channel=True, connect=False)}
        if bot_member:
            overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True)
        else:
            LOGGER.warning("Bot member not resolved; using limited overwrites.")

        now = utc_now()
        structural_change = False
        for event in list(self.events):
            if event["type"] == "one-time":
                _, delete_after_grace = self._one_time_suffix(event, now)
                if delete_after_grace:
                    structural_change = await self._prune_one_time_event(guild, event) or structural_change
                    continue

            if not event.get("enabled", True):
                had_channel = event.get("channel_id") is not None
                deleted = await self._delete_event_channel(guild, event, reason="Event disabled", clear_state=True)
                if deleted and had_channel:
                    structural_change = True
                continue

            channel_id = event.get("channel_id")
            channel = guild.get_channel(channel_id) if isinstance(channel_id, int) else None
            if isinstance(channel, discord.VoiceChannel):
                continue

            category = self._resolve_category(guild, event)
            try:
                channel = await guild.create_voice_channel(
                    name=self._compose_channel_name(event["name"], "Loading..."),
                    category=category,
                    overwrites=overwrites,
                    reason="Create event tracker channels",
                )
                self._set_channel_id(event["key"], channel.id)
                LOGGER.info("Created channel for %s -> %s", event["name"], channel.id)
                structural_change = True
                await asyncio.sleep(1)
            except (discord.Forbidden, discord.HTTPException) as exc:
                LOGGER.warning("Failed creating channel for %s: %s", event["name"], exc)
        return structural_change

    async def refresh_all_channels(self, guild: discord.Guild) -> bool:
        now = utc_now()
        structural_change = False
        for event in list(self.events):
            if event["type"] == "one-time":
                _, delete_after_grace = self._one_time_suffix(event, now)
                if delete_after_grace:
                    structural_change = await self._prune_one_time_event(guild, event) or structural_change
                    continue

            if not event.get("enabled", True):
                had_channel = event.get("channel_id") is not None
                deleted = await self._delete_event_channel(guild, event, reason="Event disabled", clear_state=True)
                if deleted and had_channel:
                    structural_change = True
                continue

            channel = guild.get_channel(event["channel_id"]) if isinstance(event.get("channel_id"), int) else None
            if not isinstance(channel, discord.VoiceChannel):
                continue

            try:
                desired_category = self._resolve_category(guild, event)
                desired_category_id = desired_category.id if desired_category else None
                if channel.category_id != desired_category_id:
                    await channel.move(
                        end=True,
                        category=desired_category,
                        reason="Update event tracker categories",
                    )

                if event["type"] == "counter":
                    count = self._counter_value(guild, event)
                    suffix = str(count) if count is not None else "N/A"
                elif event["type"] == "recurring":
                    suffix = self._recurring_suffix(event, now)
                else:
                    suffix, _ = self._one_time_suffix(event, now)

                name = self._compose_channel_name(event["name"], suffix)
                if channel.name != name:
                    await channel.edit(name=name, reason="Update event statistics")
            except discord.Forbidden:
                LOGGER.warning("Missing permissions editing %s", getattr(channel, "name", event["name"]))
            except discord.HTTPException as exc:
                if self._is_channel_edit_rate_limited(exc):
                    LOGGER.info("HTTP error editing %s: %s", getattr(channel, "name", event["name"]), exc)
                    continue
                LOGGER.warning("HTTP error editing %s: %s", getattr(channel, "name", event["name"]), exc)
            except (TypeError, ValueError, KeyError, RuntimeError):
                LOGGER.exception("Error updating %s", event["name"])

        await self._reorder_channels(guild, now)
        return structural_change

    async def _reorder_channels(self, guild: discord.Guild, now: datetime) -> None:
        grouped: dict[int | None, list[tuple[tuple[int, int, float, str], discord.VoiceChannel, dict[str, Any]]]] = {}

        for event in self.events:
            if not event.get("enabled", True):
                continue
            channel_id = event.get("channel_id")
            channel = guild.get_channel(channel_id) if isinstance(channel_id, int) else None
            if not isinstance(channel, discord.VoiceChannel):
                continue
            category = self._resolve_category(guild, event)
            category_id = category.id if category else None
            grouped.setdefault(category_id, []).append((self._sort_key_for_event(event, now), channel, event))

        for category_id, entries in grouped.items():
            entries.sort(key=lambda item: item[0])
            await self._reorder_channel_group(guild, category_id, entries)

    async def _reorder_channel_group(
        self,
        guild: discord.Guild,
        category_id: int | None,
        entries: list[tuple[tuple[int, int, float, str], discord.VoiceChannel, dict[str, Any]]],
    ) -> None:
        category = guild.get_channel(category_id) if isinstance(category_id, int) else None
        desired_channels = [channel for _, channel, _ in entries]
        desired_ids = [channel.id for channel in desired_channels]

        voice_channels = [
            channel
            for channel in guild.channels
            if isinstance(channel, discord.VoiceChannel) and channel.category_id == category_id
        ]
        voice_channels.sort(key=lambda channel: (channel.position, channel.id))

        current_ids = [channel.id for channel in voice_channels if channel.id in desired_ids]
        already_in_category = all(channel.category_id == category_id for channel in desired_channels)
        if current_ids == desired_ids and already_in_category:
            return

        anchor_positions = [index for index, channel in enumerate(voice_channels) if channel.id in desired_ids]
        anchor = min(anchor_positions) if anchor_positions else len(voice_channels)

        previous: discord.VoiceChannel | None = None
        for channel in desired_channels:
            try:
                if previous is None:
                    await channel.move(
                        beginning=True,
                        offset=anchor,
                        category=category,
                        reason="Reorder event trackers",
                    )
                else:
                    await channel.move(
                        after=previous,
                        category=category,
                        reason="Reorder event trackers",
                    )
                previous = channel
            except (discord.Forbidden, discord.HTTPException) as exc:
                if isinstance(exc, discord.HTTPException) and self._is_channel_edit_rate_limited(exc):
                    LOGGER.info("Failed to reorder channel %s: %s", channel.id, exc)
                    continue
                LOGGER.warning("Failed to reorder channel %s: %s", channel.id, exc)
