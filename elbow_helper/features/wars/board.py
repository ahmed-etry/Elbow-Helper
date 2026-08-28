"""Persistent regular-war board lifecycle."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from datetime import timezone
import logging
from typing import Any

import discord

from elbow_helper.configuration.channels import CLAN_WAR_CHANNELS
from elbow_helper.configuration.clans import CLAN_CODES_BY_NAME
from .config import WAR_BOARD_CLAN_CODES
from .helpers import build_war_id
from .rendering import build_war_board_embed
from .rendering import coc_time_to_datetime
from .rendering import normalize_war_state


LOGGER = logging.getLogger(__name__)
PREVIOUS_WAR_CUSTOM_ID_PREFIX = "war_board:previous:"


def _embed_without_timestamp(embed: discord.Embed) -> dict[str, Any]:
    payload = embed.to_dict()
    payload.pop("timestamp", None)
    for key in ("thumbnail", "image"):
        media = payload.get(key)
        if isinstance(media, dict):
            for dynamic_key in ("proxy_url", "height", "width"):
                media.pop(dynamic_key, None)
    return payload


def _merge_war_log_result(
    snapshot: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Apply final war-log aggregates without losing the captured lineups."""
    merged = deepcopy(snapshot)
    merged["state"] = "warEnded"
    for key in ("teamSize", "attacksPerMember", "endTime"):
        if result.get(key) is not None:
            merged[key] = result[key]
    for side in ("clan", "opponent"):
        source = result.get(side)
        if not isinstance(source, dict):
            continue
        target = merged.setdefault(side, {})
        for key in (
            "tag",
            "name",
            "badgeUrls",
            "stars",
            "attacks",
            "destructionPercentage",
        ):
            if source.get(key) is not None:
                target[key] = source[key]
    return merged


class PreviousWarView(discord.ui.View):
    """Persistent access to a clan's previous completed regular war."""

    def __init__(self, manager: Any, clan_code: str) -> None:
        super().__init__(timeout=None)
        self.manager = manager
        self.clan_code = clan_code
        button = discord.ui.Button(
            label="Previous War",
            style=discord.ButtonStyle.secondary,
            custom_id=f"{PREVIOUS_WAR_CUSTOM_ID_PREFIX}{clan_code}",
        )
        button.callback = self._show_previous
        self.add_item(button)

    async def _show_previous(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        snapshot = self.manager._previous_war_snapshot(self.clan_code)
        if snapshot is None:
            await interaction.edit_original_response(
                content="No previous war is available.",
                embed=None,
                view=None,
            )
            return
        emojis = await self.manager.war_emojis.get()
        embed = build_war_board_embed(
            snapshot,
            emojis,
            timestamp=coc_time_to_datetime(snapshot.get("endTime")),
        )
        await interaction.edit_original_response(
            content=None,
            embed=embed,
            view=None,
        )


class WarBoardMixin:
    def _previous_war_snapshot(self, clan_code: str) -> dict[str, Any] | None:
        history = getattr(self, "war_board_history", {})
        entry = history.get(clan_code) if isinstance(history, dict) else None
        previous = entry.get("previous") if isinstance(entry, dict) else None
        if (
            isinstance(previous, dict)
            and normalize_war_state(previous.get("state")) == "warended"
        ):
            return deepcopy(previous)
        return None

    def _war_board_view(
        self,
        clan_code: str,
        state: str,
    ) -> PreviousWarView | None:
        if state not in {"preparation", "inwar"}:
            return None
        if self._previous_war_snapshot(clan_code) is None:
            return None
        return PreviousWarView(self, clan_code)

    def _register_war_board_views(self) -> None:
        for clan_code, entry in self.war_board_registry.items():
            history = self.war_board_history.get(clan_code, {})
            current = history.get("current")
            if not isinstance(current, dict):
                continue
            state = normalize_war_state(current.get("state"))
            view = self._war_board_view(clan_code, state)
            message_id = entry.get("message")
            if view is not None and isinstance(message_id, int):
                self.bot.add_view(view, message_id=message_id)

    async def _record_war_board_snapshot(
        self,
        clan_name: str,
        clan_code: str,
        data: dict[str, Any],
    ) -> None:
        history = getattr(self, "war_board_history", None)
        if not isinstance(history, dict):
            history = {}
            self.war_board_history = history
        original = deepcopy(history.get(clan_code, {}))
        entry = history.setdefault(clan_code, {})
        current = entry.get("current")
        incoming = deepcopy(data)

        if isinstance(current, dict) and build_war_id(current) != build_war_id(incoming):
            previous: dict[str, Any] | None = None
            if normalize_war_state(current.get("state")) == "warended":
                previous = deepcopy(current)
            else:
                fetch_result = getattr(self, "_fetch_war_log_result", None)
                if callable(fetch_result):
                    result = await fetch_result(clan_name, current)
                    if isinstance(result, dict):
                        previous = _merge_war_log_result(current, result)
            if previous is None:
                entry.pop("previous", None)
            else:
                entry["previous"] = previous

        entry["current"] = incoming
        if entry != original:
            self.cache["war_board_history"] = history
            await self._save_cache_async()

    @staticmethod
    def _message_control_ids(message: discord.Message) -> set[str]:
        custom_ids: set[str] = set()
        for row in message.components:
            for child in getattr(row, "children", ()) or ():
                custom_id = getattr(child, "custom_id", None)
                if isinstance(custom_id, str):
                    custom_ids.add(custom_id)
        return custom_ids

    async def _war_board_channel(
        self,
        clan_code: str,
    ) -> discord.abc.Messageable | None:
        channel_id = CLAN_WAR_CHANNELS.get(clan_code)
        if channel_id is None:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel if hasattr(channel, "send") else None
        try:
            fetched = await self.bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return fetched if hasattr(fetched, "send") else None

    async def _registered_war_board_message(
        self,
        clan_code: str,
        channel: discord.abc.Messageable,
    ) -> tuple[discord.Message | None, bool]:
        entry = self.war_board_registry.get(clan_code)
        channel_id = getattr(channel, "id", None)
        if not entry or entry.get("channel") != channel_id:
            return None, True
        if not hasattr(channel, "fetch_message"):
            return None, False
        try:
            return await channel.fetch_message(entry["message"]), False
        except discord.NotFound:
            return None, True
        except (discord.Forbidden, discord.HTTPException):
            return None, False

    async def _save_war_board_message(
        self,
        clan_code: str,
        message: discord.Message,
    ) -> None:
        self.war_board_registry[clan_code] = {
            "channel": message.channel.id,
            "message": message.id,
        }
        self.cache["war_board_messages"] = self.war_board_registry
        await self._save_cache_async()

    async def _remove_war_board_controls(self, clan_code: str) -> None:
        channel = await self._war_board_channel(clan_code)
        if channel is None:
            return
        message, _ = await self._registered_war_board_message(clan_code, channel)
        if message is None or not message.components:
            return
        try:
            await message.edit(view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    async def _update_war_board(self, clan_name: str, data: dict[str, Any]) -> None:
        clan_code = CLAN_CODES_BY_NAME.get(clan_name)
        if clan_code not in WAR_BOARD_CLAN_CODES:
            return
        if data.get("warTag"):
            await self._remove_war_board_controls(clan_code)
            return
        state = normalize_war_state(data.get("state"))
        if state == "notinwar":
            await self._remove_war_board_controls(clan_code)
            return
        if state not in {"preparation", "inwar", "warended"}:
            return
        await self._record_war_board_snapshot(clan_name, clan_code, data)

        channel = await self._war_board_channel(clan_code)
        if channel is None:
            return
        message, can_create = await self._registered_war_board_message(
            clan_code,
            channel,
        )
        emojis = await self.war_emojis.get()
        embed = build_war_board_embed(data, emojis)
        view = self._war_board_view(clan_code, state)
        if message is None:
            if not can_create:
                return
            embed.timestamp = datetime.now(timezone.utc)
            try:
                message = await channel.send(embed=embed, view=view)
            except (discord.Forbidden, discord.HTTPException) as error:
                LOGGER.warning(
                    "War board could not be posted for %s: %s",
                    clan_code,
                    error,
                )
                return
            await self._save_war_board_message(clan_code, message)
            return

        content_changed = not message.embeds or (
            _embed_without_timestamp(message.embeds[0])
            != _embed_without_timestamp(embed)
        )
        expected_control_ids = (
            {f"{PREVIOUS_WAR_CUSTOM_ID_PREFIX}{clan_code}"}
            if view is not None
            else set()
        )
        existing_control_ids = self._message_control_ids(message)
        controls_changed = (
            existing_control_ids != expected_control_ids
            if expected_control_ids
            else bool(message.components)
        )
        if not content_changed and not controls_changed:
            return
        if content_changed:
            embed.timestamp = datetime.now(timezone.utc)
        elif message.embeds:
            embed.timestamp = message.embeds[0].timestamp
        try:
            await message.edit(embed=embed, view=view)
        except discord.NotFound:
            self.war_board_registry.pop(clan_code, None)
            self.cache["war_board_messages"] = self.war_board_registry
            await self._save_cache_async()
        except (discord.Forbidden, discord.HTTPException) as error:
            LOGGER.warning(
                "War board could not be updated for %s: %s",
                clan_code,
                error,
            )
