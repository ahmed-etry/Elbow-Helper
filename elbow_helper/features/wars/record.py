"""Boards, summaries, roles and queries from the family record.

The collectors write every war check to the record. Each finished collection
pass refreshes the war features from the record's war reads; the features
make no Clash requests of their own. A record war is turned back into the
Clash-shaped payload the boards, summaries, roles and queries use. Stale,
failed or missing evidence changes nothing: boards stay, roles stay and
summaries wait.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import sqlite3
from typing import Any

from elbow_helper.domain.war_views import board_payload, record_fresh

import discord

from elbow_helper.configuration.clans import CLAN_CODES_BY_NAME

from . import config
from .helpers import build_war_id, missed_attack_line
from elbow_helper.infrastructure.discord.war_boards import coc_time_to_datetime
from elbow_helper.domain.wars import normalize_war_state

LOGGER = logging.getLogger(__name__)
SECOND = 1_000_000
ACTIVE = ("preparation", "inWar")


def _observed_at(value: int) -> str:
    return datetime.fromtimestamp(value / SECOND, timezone.utc).isoformat()


class RecordMixin:
    """Drive the war features from the record after each collection pass."""

    async def _sync_war_state_from_record(self) -> None:
        """Startup replay: restore boards, roles and transitions without replaying summaries."""
        await self.bot.wait_until_ready()
        try:
            await self._refresh_from_record(startup=True)
        finally:
            self._startup_sync_done.set()

    async def _refresh_from_record(self, *, startup: bool = False) -> None:
        if self.war_reads is None:
            LOGGER.warning("War refresh skipped: the family record's war reads are unavailable")
            return
        async with self._record_lock:
            for clan_name, clan_tag in config.CLAN_TAGS.items():
                try:
                    await self._refresh_clan_from_record(clan_name, clan_tag, startup=startup)
                except asyncio.CancelledError:
                    raise
                except (discord.HTTPException, OSError, sqlite3.Error, RuntimeError,
                        TypeError, ValueError, KeyError) as error:
                    # Retry on the next pass even if no newer check arrives.
                    self._record_checked.pop(clan_name, None)
                    LOGGER.exception("War refresh from the family record failed for %s: %s", clan_name, error)

    async def _refresh_clan_from_record(self, clan_name: str, clan_tag: str, *, startup: bool) -> None:
        clan_code = CLAN_CODES_BY_NAME.get(clan_name)
        current = await self.war_reads.current(clan_tag, lineups=True)
        war = current["war"]
        if not record_fresh(current) or current["state"] != "notInWar" and war is None:
            # Nothing is acted on; the queries say the board is unverified.
            self.war_observations.pop(clan_code, None)
            return
        if not startup and current["checked_at"] == self._record_checked.get(clan_name):
            return
        payload = board_payload(war) if war is not None else {"state": "notInWar"}
        state = normalize_war_state(payload.get("state"))
        if clan_code in config.WAR_BOARD_CLAN_CODES:
            board = self.record_boards.setdefault(clan_code, {})
            if war is not None and not war["war_tag"]:
                board["current"] = payload
            previous = (await self.war_reads.previous(clan_tag, lineups=True))["war"]
            if previous is not None and previous["state"] == "warEnded":
                board["previous"] = board_payload(previous)
            else:
                board.pop("previous", None)
            self.war_observations[clan_code] = {
                "state": "cwl" if payload.get("warTag") else state,
                "observed_at": _observed_at(current["checked_at"]),
                "war_id": build_war_id(payload) if not payload.get("warTag") and state != "notinwar" else None,
            }
            if clan_code not in self._record_views:
                self._register_war_board_view(clan_code)
                self._record_views.add(clan_code)
        await self._sync_record_roles(clan_name, payload, war)
        await self._update_war_board(clan_name, payload)
        await self._handle_war_state(clan_name, payload)
        self._record_checked[clan_name] = current["checked_at"]

    async def _sync_record_roles(self, clan_name: str, payload: dict[str, Any], war: dict[str, Any] | None) -> None:
        # A partial lineup must not remove anyone's role.
        if (war is not None and not war["war_tag"] and war["state"] in ACTIVE
                and not war["coverage"]["own"]["lineup_complete"]):
            LOGGER.warning("War role sync skipped for %s: the recorded lineup is incomplete", clan_name)
            return
        await self._sync_war_roles(clan_name, payload)

    async def _missed_attack_lines(self, clan: str, data: dict[str, Any]) -> list[str]:
        """The record's named misses; until it knows every attack, the lineup's, as the summary always showed."""
        lines = await self._recorded_missed_lines(data)
        return self._compute_missed(data) if lines is None else lines

    async def _recorded_missed_lines(self, data: dict[str, Any]) -> list[str] | None:
        """Named missed attacks of the ended war, once the record knows every attack."""
        clan_tag = (data.get("clan") or {}).get("tag")
        opponent_tag = (data.get("opponent") or {}).get("tag")
        ended = coc_time_to_datetime(data.get("endTime"))
        if not clan_tag or ended is None:
            return None
        end = round(ended.timestamp() * SECOND)
        result = await self.war_reads.completed([clan_tag], end - SECOND, end + SECOND, types=("FRIENDLY", "REG"))
        row = next((row for row in result["rows"] if row["opponent"]["clan_tag"] == opponent_tag), None)
        if row is None or row["missed_attacks"] is None:
            return None
        return [missed_attack_line(miss["player_name"], miss["missed"]) for miss in row["missed_attacks"]]
