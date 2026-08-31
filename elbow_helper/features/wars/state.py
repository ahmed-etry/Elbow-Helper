"""Cache persistence and in-memory state helpers"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Dict

from elbow_helper.infrastructure.persistence import read_json
from elbow_helper.infrastructure.persistence import write_json_atomic
from .config import CACHE_FILE, PROCESSED_WAR_RETENTION

LOGGER = logging.getLogger(__name__)
CACHE_WRITE_LOCK = threading.Lock()


def load_cache() -> Dict[str, Any]:
    # Load persisted state (processed wars and summaries)
    try:
        data = read_json(CACHE_FILE)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except OSError as e:
        LOGGER.warning("Failed to load cache %s: %s", CACHE_FILE, e)
        return {}


def save_cache(cache: Dict[str, Any]) -> None:
    with CACHE_WRITE_LOCK:
        try:
            write_json_atomic(CACHE_FILE, cache, indent=2)
        except (OSError, TypeError) as e:
            LOGGER.error("Failed to save cache %s: %s", CACHE_FILE, e)
            raise


class StateMixin:

    def _load_war_board_history(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        raw_history = self.cache.get("war_board_history")
        if not isinstance(raw_history, dict):
            return {}

        history: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for clan_code, entry in raw_history.items():
            if not isinstance(clan_code, str) or not isinstance(entry, dict):
                continue
            normalized: Dict[str, Dict[str, Any]] = {}
            for key in ("current", "previous"):
                snapshot = entry.get(key)
                if (
                    isinstance(snapshot, dict)
                    and isinstance(snapshot.get("clan"), dict)
                    and isinstance(snapshot.get("opponent"), dict)
                    and isinstance(snapshot.get("state"), str)
                ):
                    normalized[key] = snapshot
            if normalized:
                history[clan_code] = normalized

        if history != raw_history:
            if history:
                self.cache["war_board_history"] = history
            else:
                self.cache.pop("war_board_history", None)
            save_cache(self.cache)
        return history

    def _load_war_role_state(
        self,
    ) -> tuple[Dict[str, Dict[str, int]], Dict[str, set[int]]]:
        raw_state = self.cache.get("war_role_state")
        if not isinstance(raw_state, dict):
            return {}, {}

        lineups: Dict[str, Dict[str, int]] = {}
        managed_members: Dict[str, set[int]] = {}
        for clan_code, entry in raw_state.items():
            if not isinstance(clan_code, str) or not isinstance(entry, dict):
                continue
            raw_lineup = entry.get("lineup")
            if isinstance(raw_lineup, dict):
                lineup = {
                    str(tag): member_id
                    for tag, member_id in raw_lineup.items()
                    if isinstance(tag, str)
                    and tag.startswith("#")
                    and isinstance(member_id, int)
                    and member_id > 0
                }
                if lineup:
                    lineups[clan_code] = lineup
            raw_managed = entry.get("managed")
            if isinstance(raw_managed, list):
                managed = {
                    member_id
                    for member_id in raw_managed
                    if isinstance(member_id, int) and member_id > 0
                }
                if managed:
                    managed_members[clan_code] = managed
        return lineups, managed_members

    def _store_war_role_state(self) -> None:
        clan_codes = set(self.war_role_lineups) | set(self.war_role_managed_members)
        state: Dict[str, Dict[str, Any]] = {}
        for clan_code in sorted(clan_codes):
            lineup = self.war_role_lineups.get(clan_code, {})
            managed = self.war_role_managed_members.get(clan_code, set())
            if lineup or managed:
                state[clan_code] = {
                    "lineup": dict(sorted(lineup.items())),
                    "managed": sorted(managed),
                }
        if state:
            self.cache["war_role_state"] = state
        else:
            self.cache.pop("war_role_state", None)

    async def _save_cache_async(self) -> None:
        await asyncio.to_thread(save_cache, self.cache)

    def _prune_processed_wars(self) -> bool:
        overflow = len(self.processed_war_order) - PROCESSED_WAR_RETENTION
        if overflow <= 0:
            self.cache["processed_wars"] = list(self.processed_war_order)
            return False
        for stale_war_id in self.processed_war_order[:overflow]:
            self.processed_war_ids.discard(stale_war_id)
        self.processed_war_order = self.processed_war_order[overflow:]
        self.cache["processed_wars"] = list(self.processed_war_order)
        return True

    def _record_processed_war(self, war_id: str) -> None:
        if war_id in self.processed_war_ids:
            return
        self.processed_war_ids.add(war_id)
        self.processed_war_order.append(war_id)
        self._prune_processed_wars()

    def _load_summary_registry(self) -> Dict[str, Dict[str, int]]:
        # Load and normalize summary registry.
        registry = self.cache.get("summary_messages")
        if not isinstance(registry, dict):
            return {}

        normalized: Dict[str, Dict[str, int]] = {}
        for msg_id, entry in registry.items():
            if not isinstance(entry, dict):
                continue
            chan_id = entry.get("channel")
            sent_at = entry.get("sent_at")
            if isinstance(chan_id, int) and isinstance(sent_at, int):
                normalized[str(msg_id)] = {"channel": chan_id, "sent_at": sent_at}

        if normalized != registry:
            self.cache["summary_messages"] = normalized
            save_cache(self.cache)
        return normalized

    def _load_war_board_registry(self) -> Dict[str, Dict[str, int]]:
        registry = self.cache.get("war_board_messages")
        if not isinstance(registry, dict):
            return {}

        normalized: Dict[str, Dict[str, int]] = {}
        for clan_code, entry in registry.items():
            if not isinstance(clan_code, str) or not isinstance(entry, dict):
                continue
            channel_id = entry.get("channel")
            message_id = entry.get("message")
            if isinstance(channel_id, int) and isinstance(message_id, int):
                normalized[clan_code] = {
                    "channel": channel_id,
                    "message": message_id,
                }

        if normalized != registry:
            self.cache["war_board_messages"] = normalized
            save_cache(self.cache)
        return normalized
