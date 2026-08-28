"""Event stats lifecycle, state wiring, and operator helpers."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any
from typing import Optional
from uuid import uuid4

import discord
from elbow_helper.discord.pagination import format_page_footer
from discord.ext import commands

from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.configuration.roles import LEAD
from elbow_helper.domain.timezones import format_timezone_display
from elbow_helper.infrastructure.time import UTC
from elbow_helper.infrastructure.time import utc_now

from .channels import EventStatsChannelsMixin
from .commands import EventStatsCommandsMixin
from .config import DEFAULT_GRACE_HOURS
from .config import EVENT_LIST_PAGE_SIZE
from .config import HIGH_PRECISION_REFRESH_INTERVAL_SECONDS
from .config import POINT_FUNCTIONS
from .config import REFRESH_INTERVAL_SECONDS
from .config import RANGE_FUNCTIONS
from .config import get_preset_definition
from .state import ensure_state
from .state import save_state

LOGGER = logging.getLogger(__name__)


class EventStatsCog(EventStatsCommandsMixin, EventStatsChannelsMixin, commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._startup_task: Optional[asyncio.Task] = None
        self._refresh_lock = asyncio.Lock()
        self.state = ensure_state()
        self.events: list[dict[str, Any]] = []
        self._reload_events_from_state()
        self._startup_task = asyncio.create_task(self._startup())

    def cog_unload(self) -> None:
        if self._startup_task and not self._startup_task.done():
            self._startup_task.cancel()

    def _state_events(self) -> list[dict[str, Any]]:
        events = self.state.get("events")
        if not isinstance(events, list):
            self.state["events"] = []
            return self.state["events"]
        return events

    def _find_state_event(self, key: str) -> dict[str, Any] | None:
        for event in self._state_events():
            if event.get("key") == key:
                return event
        return None

    def get_event(self, key: str) -> dict[str, Any] | None:
        for event in self.events:
            if event.get("key") == key:
                return event
        return None

    def _persist_state(self) -> None:
        save_state(self.state)
        self._reload_events_from_state()

    def _reload_events_from_state(self) -> None:
        loaded_events: list[dict[str, Any]] = []

        for raw_event in self._state_events():
            key = str(raw_event.get("key") or "").strip()
            source = str(raw_event.get("source") or "").strip().lower()
            if not key:
                continue

            if source == "preset":
                preset = get_preset_definition(key)
                if preset is None:
                    LOGGER.warning("Unknown preset event key in state: %s", key)
                    continue

                event = {
                    "key": key,
                    "source": "preset",
                    "type": preset["type"],
                    "enabled": bool(raw_event.get("enabled", True)),
                    "name": str(raw_event.get("name") or preset["name"]).strip() or preset["name"],
                    "grace_period_hours": int(raw_event.get("grace_period_hours", preset["grace_period_hours"])),
                    "category_id": raw_event.get("category_id"),
                    "channel_id": raw_event.get("channel_id"),
                    "position": int(raw_event.get("position", 0)),
                }
                if event["type"] == "counter":
                    event["roles_to_count"] = list(preset["roles_to_count"])
                elif event["type"] == "recurring":
                    schedule_shape = str(preset.get("schedule_shape") or "range")
                    schedule_name = str(preset["schedule_name"])
                    event["schedule_shape"] = schedule_shape
                    event["schedule_name"] = schedule_name
                    if schedule_shape == "point":
                        event["point_fn"] = POINT_FUNCTIONS[schedule_name]
                    else:
                        event["range_fn"] = RANGE_FUNCTIONS[schedule_name]
                loaded_events.append(event)
                continue

            if str(raw_event.get("type") or "").strip().lower() != "one-time":
                LOGGER.warning("Unknown custom event type in state: %s", raw_event)
                continue

            try:
                start = datetime.fromisoformat(str(raw_event["start"]))
                end = datetime.fromisoformat(str(raw_event["end"]))
            except (KeyError, TypeError, ValueError) as exc:
                LOGGER.warning("Failed to load one-time event %s: %s", raw_event, exc)
                continue

            if start.tzinfo is None:
                start = start.replace(tzinfo=UTC)
            else:
                start = start.astimezone(UTC)
            if end.tzinfo is None:
                end = end.replace(tzinfo=UTC)
            else:
                end = end.astimezone(UTC)

            loaded_events.append(
                {
                    "key": key,
                    "source": "custom",
                    "type": "one-time",
                    "enabled": bool(raw_event.get("enabled", True)),
                    "name": str(raw_event.get("name") or "Event").strip() or "Event",
                    "start": start,
                    "end": end,
                    "timezone": str(raw_event.get("timezone") or "UTC").strip() or "UTC",
                    "grace_period_hours": int(raw_event.get("grace_period_hours", DEFAULT_GRACE_HOURS)),
                    "category_id": raw_event.get("category_id"),
                    "channel_id": raw_event.get("channel_id"),
                    "position": int(raw_event.get("position", 0)),
                }
            )

        loaded_events.sort(key=lambda item: (int(item.get("position", 0)), item["key"]))
        self.events = loaded_events

    def _can_manage(self, user: discord.abc.User) -> bool:
        roles = getattr(user, "roles", [])
        return any(role.id in LEAD for role in roles)

    def _build_custom_key(self, name: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_") or "event"
        return f"event_{slug}_{uuid4().hex[:8]}"

    def create_one_time_event(
        self,
        *,
        name: str,
        start: datetime,
        end: datetime,
        timezone: str,
        grace_hours: int,
    ) -> str:
        key = self._build_custom_key(name)
        self._state_events().append(
            {
                "key": key,
                "source": "custom",
                "type": "one-time",
                "enabled": True,
                "name": name,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "timezone": timezone,
                "grace_period_hours": grace_hours,
                "category_id": None,
                "channel_id": None,
                "position": len(self._state_events()),
            }
        )
        self._persist_state()
        return key

    def update_one_time_event(
        self,
        key: str,
        *,
        name: str,
        start: datetime,
        end: datetime,
        timezone: str,
        grace_hours: int,
    ) -> bool:
        state_event = self._find_state_event(key)
        if state_event is None or state_event.get("source") != "custom":
            return False
        state_event["name"] = name
        state_event["start"] = start.isoformat()
        state_event["end"] = end.isoformat()
        state_event["timezone"] = timezone
        state_event["grace_period_hours"] = grace_hours
        self._persist_state()
        return True

    def update_preset_event(self, key: str, *, name: str, grace_hours: int | None = None) -> bool:
        state_event = self._find_state_event(key)
        if state_event is None or state_event.get("source") != "preset":
            return False
        preset = get_preset_definition(key)
        if preset is None:
            return False
        state_event["name"] = name
        if grace_hours is not None and preset["type"] != "counter":
            state_event["grace_period_hours"] = grace_hours
        self._persist_state()
        return True

    def reset_preset_event(self, key: str) -> bool:
        state_event = self._find_state_event(key)
        preset = get_preset_definition(key)
        if state_event is None or state_event.get("source") != "preset" or preset is None:
            return False
        state_event["enabled"] = True
        state_event["name"] = preset["name"]
        state_event["grace_period_hours"] = int(preset["grace_period_hours"])
        state_event["category_id"] = None
        self._persist_state()
        return True

    def toggle_event(self, key: str) -> bool:
        state_event = self._find_state_event(key)
        if state_event is None:
            return False
        state_event["enabled"] = not bool(state_event.get("enabled", True))
        self._persist_state()
        return True

    def set_event_category(self, key: str, category_id: int | None) -> bool:
        state_event = self._find_state_event(key)
        if state_event is None:
            return False
        state_event["category_id"] = category_id
        self._persist_state()
        return True

    def move_event(self, key: str, direction: int) -> bool:
        ordered = sorted(self._state_events(), key=lambda item: (int(item.get("position", 0)), str(item.get("key") or "")))
        index = next((i for i, event in enumerate(ordered) if event.get("key") == key), None)
        if index is None:
            return False

        target_index = index + direction
        if target_index < 0 or target_index >= len(ordered):
            return False

        ordered[index], ordered[target_index] = ordered[target_index], ordered[index]
        for position, event in enumerate(ordered):
            event["position"] = position
        self.state["events"] = ordered
        self._persist_state()
        return True

    async def delete_custom_event(self, guild: discord.Guild, key: str) -> tuple[bool, str]:
        event = self.get_event(key)
        state_event = self._find_state_event(key)
        if event is None or state_event is None or state_event.get("source") != "custom":
            return False, "Event not found."

        channel_id = event.get("channel_id")
        channel = guild.get_channel(channel_id) if isinstance(channel_id, int) else None
        if isinstance(channel, discord.VoiceChannel):
            try:
                await channel.delete(reason="Custom event removed from event panel")
            except discord.Forbidden:
                return False, "I do not have permission to delete that channel."
            except discord.HTTPException:
                LOGGER.exception(
                    "Failed deleting voice channel %s for custom event %s",
                    channel.id,
                    key,
                )
                return False, "I couldn't delete the event's voice channel right now. Try again in a moment."

        self.state["events"] = [item for item in self._state_events() if item.get("key") != key]
        self._persist_state()
        return True, "Event removed."

    async def force_refresh(self, guild: discord.Guild) -> None:
        async with self._refresh_lock:
            self._reload_events_from_state()
            structural_change = await self._ensure_channels(guild)
            structural_change = await self.refresh_all_channels(guild) or structural_change
            if structural_change:
                # Newly created/recreated channels can land at the end of the list.
                # Run one extra reorder pass after Discord settles the channel state.
                await asyncio.sleep(1)
                await self._reorder_channels(guild, utc_now())

    def describe_event_type(self, event: dict[str, Any]) -> str:
        if event["type"] == "counter":
            return "Counter"
        if event["type"] == "recurring":
            return "Recurring"
        return "One-time"

    def _category_label(self, guild: discord.Guild | None, event: dict[str, Any]) -> str:
        category_id = event.get("category_id")
        if not isinstance(category_id, int) or guild is None:
            return "No category"
        category = guild.get_channel(category_id)
        if isinstance(category, discord.CategoryChannel):
            return category.name
        return "Missing category"

    def _channel_label(self, guild: discord.Guild | None, event: dict[str, Any]) -> str:
        channel_id = event.get("channel_id")
        if not isinstance(channel_id, int) or guild is None:
            return "Not created"
        channel = guild.get_channel(channel_id)
        return channel.mention if isinstance(channel, discord.VoiceChannel) else "Missing channel"

    def _schedule_lines(self, event: dict[str, Any]) -> list[str]:
        if event["type"] == "counter":
            return ["Updates automatically from the selected member roles."]
        if event["type"] == "recurring":
            now = utc_now()
            if self._recurring_schedule_shape(event) == "point":
                next_point = self._recurring_point(event, now)
                return [
                    "Tracks the next scheduled date.",
                    f"Next: <t:{int(next_point.timestamp())}:F> (<t:{int(next_point.timestamp())}:R>)",
                ]

            start, end = self._recurring_range(event, now)
            is_live = start <= now < end
            lines = ["Tracks participation from the start time through the end time."]
            window_label = "Active Window" if is_live else "Next Window"
            edge_label = "Ends" if is_live else "Starts"
            edge_ts = int(end.timestamp()) if is_live else int(start.timestamp())
            lines.append(f"{window_label}: <t:{int(start.timestamp())}:F> to <t:{int(end.timestamp())}:F>")
            lines.append(f"{edge_label}: <t:{edge_ts}:R>")
            return lines

        start = event["start"]
        end = event["end"]
        timezone_name = str(event.get("timezone") or "UTC")
        return [
            f"Timezone: {format_timezone_display(timezone_name)}",
            f"Start: <t:{int(start.timestamp())}:F>",
            f"End: <t:{int(end.timestamp())}:F>",
        ]

    def build_panel_embed(self, guild: discord.Guild | None) -> discord.Embed:
        now = utc_now()
        enabled = [event for event in self.events if event.get("enabled", True)]
        live = [event for event in enabled if self._event_phase(event, now) == "live"]
        upcoming = [event for event in enabled if self._event_phase(event, now) == "upcoming"]
        disabled = [event for event in self.events if not event.get("enabled", True)]

        embed = discord.Embed(
            title="Events",
            description="Create and manage event trackers.",
            color=discord.Color.blurple(),
            timestamp=now,
        )
        embed.add_field(
            name="Summary",
            value=(
                f"Enabled: **{len(enabled)}**\n"
                f"Live: **{len(live)}**\n"
                f"Upcoming: **{len(upcoming)}**\n"
                f"Disabled: **{len(disabled)}**"
            ),
            inline=True,
        )
        if guild is not None:
            visible = self.events[: min(EVENT_LIST_PAGE_SIZE, len(self.events))]
            lines = [
                f"`{index + 1:02d}` {event['name']} - {self.describe_event_status(event, guild)}"
                for index, event in enumerate(visible)
            ]
            embed.add_field(
                name="Event trackers",
                value="\n".join(lines) if lines else "No events set up yet.",
                inline=False,
            )
        embed.set_footer(text="**Create** adds a one-time event. **Manage** edits an existing event.")
        return embed

    def build_event_list_embed(self, guild: discord.Guild | None, page: int = 0) -> discord.Embed:
        total_pages = max(1, (len(self.events) + EVENT_LIST_PAGE_SIZE - 1) // EVENT_LIST_PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        start = page * EVENT_LIST_PAGE_SIZE
        end = start + EVENT_LIST_PAGE_SIZE

        embed = discord.Embed(
            title="All Events",
            description="Every event tracker and its current status.",
            color=discord.Color.blurple(),
            timestamp=utc_now(),
        )
        for event in self.events[start:end]:
            details = [
                f"Type: {self.describe_event_type(event)}",
                f"Status: {self.describe_event_status(event, guild)}",
                f"Category: {self._category_label(guild, event)}",
                f"Channel: {self._channel_label(guild, event)}",
            ]
            if event["type"] == "one-time":
                details.append(f"Timezone: {format_timezone_display(str(event.get('timezone') or 'UTC'))}")
            embed.add_field(name=event["name"], value="\n".join(details), inline=False)

        if not embed.fields:
            embed.description = "No events set up yet."
        embed.set_footer(text=format_page_footer(page + 1, total_pages))
        return embed

    def build_event_detail_embed(self, guild: discord.Guild | None, key: str) -> discord.Embed:
        event = self.get_event(key)
        embed = discord.Embed(color=discord.Color.blurple(), timestamp=utc_now())
        if event is None:
            embed.title = "Event Not Found"
            embed.description = "This event was removed or renamed."
            return embed

        embed.title = event["name"]
        embed.description = (
            f"{self.describe_event_type(event)}\n"
            f"Status: **{self.describe_event_status(event, guild)}**"
        )
        embed.add_field(name="Enabled", value="Yes" if event.get("enabled", True) else "No", inline=True)
        embed.add_field(name="Category", value=self._category_label(guild, event), inline=True)
        embed.add_field(name="Channel", value=self._channel_label(guild, event), inline=True)
        if event["type"] == "one-time" or (event["type"] == "recurring" and self._recurring_schedule_shape(event) == "range"):
            embed.add_field(name="Grace period (hours)", value=str(int(event.get("grace_period_hours", 0))), inline=True)
        embed.add_field(name="Display order", value=str(int(event.get("position", 0)) + 1), inline=True)
        embed.add_field(name="Schedule", value="\n".join(self._schedule_lines(event)), inline=False)
        return embed

    async def _startup(self) -> None:
        await self.bot.wait_until_ready()
        boot_event = getattr(self.bot, "boot_complete", None)
        if isinstance(boot_event, asyncio.Event):
            await boot_event.wait()

        guild = self._get_guild()
        if guild is None:
            LOGGER.warning("No guild available.")
            return

        await self.force_refresh(guild)

        while True:
            now = utc_now()
            refresh_interval = (
                HIGH_PRECISION_REFRESH_INTERVAL_SECONDS
                if self._requires_high_precision_refresh(now)
                else REFRESH_INTERVAL_SECONDS
            )
            await asyncio.sleep(self._seconds_until_next_refresh(now, refresh_interval))
            try:
                await self.force_refresh(guild)
            except (discord.Forbidden, discord.HTTPException, RuntimeError):
                LOGGER.exception("Event refresh failed")

    def _get_guild(self) -> Optional[discord.Guild]:
        return self.bot.get_guild(GUILD_ID) if GUILD_ID else (self.bot.guilds[0] if self.bot.guilds else None)

    @staticmethod
    def _seconds_until_next_refresh(now: datetime, interval_seconds: int) -> float:
        micros = int(now.timestamp() * 1_000_000)
        interval_micros = int(interval_seconds * 1_000_000)
        remainder = micros % interval_micros
        if remainder == 0:
            return float(interval_seconds)
        return float(interval_micros - remainder) / 1_000_000.0


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EventStatsCog(bot))
