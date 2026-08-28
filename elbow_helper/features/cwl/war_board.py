"""Persistent CWL war boards."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
from datetime import timezone
import logging
from typing import Any

import discord

from elbow_helper.features.wars.rendering import build_war_board_embed
from elbow_helper.features.wars.rendering import normalize_war_state
from elbow_helper.configuration.channels import CLAN_CWL_INFO_CHANNELS

from .config import CWL_CLAN_TAGS
from .helpers import coc_time_to_dt


LOGGER = logging.getLogger(__name__)


def _embed_without_timestamp(embed: discord.Embed) -> dict[str, Any]:
    payload = embed.to_dict()
    payload.pop("timestamp", None)
    for key in ("thumbnail", "image"):
        media = payload.get(key)
        if isinstance(media, dict):
            for dynamic_key in ("proxy_url", "height", "width"):
                media.pop(dynamic_key, None)
    return payload


def _positive_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _war_round(war: dict[str, Any]) -> int:
    return _positive_int(war.get("_round"))


def cwl_board_snapshot_is_complete(wars: list[dict[str, Any]]) -> bool:
    """Reject partial snapshots that would temporarily drop an overlapping day."""
    total_rounds = max(
        (
            _positive_int(war.get("_total_rounds"))
            for war in wars
            if war.get("_total_rounds") is not None
        ),
        default=0,
    )
    if total_rounds <= 0:
        return True

    battle_rounds = {
        _war_round(war)
        for war in wars
        if normalize_war_state(war.get("_state") or war.get("state")) == "inwar"
    }
    preparation_rounds = {
        _war_round(war)
        for war in wars
        if normalize_war_state(war.get("_state") or war.get("state"))
        == "preparation"
    }
    if battle_rounds:
        battle_round = max(battle_rounds)
        if battle_round < total_rounds and battle_round + 1 not in preparation_rounds:
            return False
    if preparation_rounds:
        preparation_round = min(preparation_rounds)
        if preparation_round > 1 and preparation_round - 1 not in battle_rounds:
            return False
    if not battle_rounds and not preparation_rounds:
        ended_rounds = {
            _war_round(war)
            for war in wars
            if normalize_war_state(war.get("_state") or war.get("state"))
            == "warended"
        }
        if ended_rounds and max(ended_rounds) < total_rounds:
            return False
    return True


def select_cwl_board_wars(wars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select battle and upcoming preparation, or the latest final result."""
    battle_wars = [
        war
        for war in wars
        if normalize_war_state(war.get("_state") or war.get("state")) == "inwar"
    ]
    preparation_wars = [
        war
        for war in wars
        if normalize_war_state(war.get("_state") or war.get("state"))
        == "preparation"
    ]

    selected: list[dict[str, Any]] = []
    if battle_wars:
        selected.append(max(battle_wars, key=_war_round))
    if preparation_wars:
        selected.append(min(preparation_wars, key=_war_round))
    if selected:
        return selected

    ended_wars = [
        war
        for war in wars
        if normalize_war_state(war.get("_state") or war.get("state"))
        == "warended"
    ]
    if not ended_wars:
        return []
    return [
        max(
            ended_wars,
            key=lambda war: (
                _war_round(war),
                coc_time_to_dt(war.get("endTime"))
                or datetime.min.replace(tzinfo=timezone.utc),
            ),
        )
    ]


def orient_cwl_war(war: dict[str, Any], clan_tag: str) -> dict[str, Any] | None:
    """Return a copy with the configured clan consistently on the left."""
    clan = war.get("clan")
    opponent = war.get("opponent")
    if not isinstance(clan, dict) or not isinstance(opponent, dict):
        return None
    oriented = deepcopy(war)
    if clan.get("tag") == clan_tag:
        return oriented
    if opponent.get("tag") == clan_tag:
        oriented["clan"], oriented["opponent"] = (
            oriented["opponent"],
            oriented["clan"],
        )
        return oriented
    return None


class CwlWarBoardMixin:
    async def _cwl_board_channel(
        self,
        clan_code: str,
    ) -> discord.TextChannel | None:
        channel_id = CLAN_CWL_INFO_CHANNELS.get(clan_code)
        if channel_id is None:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def _registered_cwl_board_message(
        self,
        clan_code: str,
        channel: discord.TextChannel,
    ) -> tuple[discord.Message | None, bool]:
        entry = self.cwl_board_registry.get(clan_code)
        if not entry or entry.get("channel") != channel.id:
            return None, True
        try:
            return await channel.fetch_message(entry["message"]), False
        except discord.NotFound:
            return None, True
        except (discord.Forbidden, discord.HTTPException):
            return None, False

    async def _save_cwl_board_message(
        self,
        clan_code: str,
        message: discord.Message,
    ) -> None:
        self.cwl_board_registry[clan_code] = {
            "channel": message.channel.id,
            "message": message.id,
        }
        self.state["cwl_board_messages"] = self.cwl_board_registry
        await self._save_state()

    async def _upsert_cwl_board(
        self,
        clan_code: str,
        channel: discord.TextChannel,
        wars: list[dict[str, Any]],
    ) -> bool:
        clan_tag = CWL_CLAN_TAGS.get(clan_code)
        if not clan_tag or not cwl_board_snapshot_is_complete(wars):
            return False
        selected = select_cwl_board_wars(wars)
        oriented = [
            payload
            for war in selected
            if (payload := orient_cwl_war(war, clan_tag)) is not None
        ]
        if not oriented:
            return False

        emojis = await self.cwl_war_emojis.get()
        embeds = [build_war_board_embed(war, emojis) for war in oriented]
        lock = self._cwl_board_locks.setdefault(clan_code, asyncio.Lock())
        async with lock:
            message, can_create = await self._registered_cwl_board_message(
                clan_code,
                channel,
            )
            if message is None:
                if not can_create:
                    return False
                updated_at = datetime.now(timezone.utc)
                for embed in embeds:
                    embed.timestamp = updated_at
                try:
                    message = await channel.send(embeds=embeds)
                except (discord.Forbidden, discord.HTTPException) as error:
                    LOGGER.warning(
                        "CWL war board could not be posted for %s: %s",
                        clan_code,
                        error,
                    )
                    return False
                await self._save_cwl_board_message(clan_code, message)
                return True

            content_changed = len(message.embeds) != len(embeds) or any(
                _embed_without_timestamp(current)
                != _embed_without_timestamp(replacement)
                for current, replacement in zip(message.embeds, embeds)
            )
            controls_changed = bool(message.components)
            if not content_changed and not controls_changed:
                return True
            if content_changed:
                updated_at = datetime.now(timezone.utc)
                for embed in embeds:
                    embed.timestamp = updated_at
            else:
                for index, embed in enumerate(embeds):
                    embed.timestamp = message.embeds[index].timestamp
            try:
                await message.edit(content=None, embeds=embeds, view=None)
            except discord.NotFound:
                self.cwl_board_registry.pop(clan_code, None)
                self.state["cwl_board_messages"] = self.cwl_board_registry
                await self._save_state()
                return False
            except (discord.Forbidden, discord.HTTPException) as error:
                LOGGER.warning(
                    "CWL war board could not be updated for %s: %s",
                    clan_code,
                    error,
                )
                return False
            return True

    async def _sync_cwl_channel(
        self,
        clan_code: str,
        wars: list[dict[str, Any]],
    ) -> None:
        channel = await self._cwl_board_channel(clan_code)
        if channel is None:
            return
        await self._upsert_cwl_board(clan_code, channel, wars)
