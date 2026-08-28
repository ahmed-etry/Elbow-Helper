"""Thread feature runtime state and persistence."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date
from datetime import datetime
from datetime import timezone as dt_timezone
from typing import Any
from typing import Dict
from typing import Optional
from typing import Set

from elbow_helper.infrastructure.persistence import read_json
from elbow_helper.infrastructure.persistence import write_json_atomic


LOGGER = logging.getLogger(__name__)


class CwlThreadStateMixin:
    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(dt_timezone.utc)


    def _utc_now_iso(self) -> str:
        return self._utc_now().isoformat()


    def init_thread_feature(self) -> None:
        self.last_message_times: Dict[str, Any] = {}
        self.conversation_active: Dict[str, bool] = {}
        self.sticky_repositioned: Dict[str, bool] = {}
        self._sticky_update_locks: Dict[str, asyncio.Lock] = {}
        self.load_data()


    def start_thread_tasks(self) -> None:
        for task_loop in (
            self.check_sticky_reposition,
            self.auto_reset_cwl,
            self.maintain_registered_thread_visibility,
            self.refresh_sticky_status,
        ):
            if not task_loop.is_running():
                task_loop.start()


    def stop_thread_tasks(self) -> None:
        for task_loop in (
            self.check_sticky_reposition,
            self.auto_reset_cwl,
            self.maintain_registered_thread_visibility,
            self.refresh_sticky_status,
        ):
            if task_loop.is_running():
                task_loop.cancel()


    def load_data(self) -> None:
        """Load CWL thread data from file."""
        try:
            if os.path.exists(self.data_file):
                self.data = read_json(self.data_file)
            else:
                self.data = {
                    "threads": {},
                    "settings": {},
                }
                self.save_data()
            if not isinstance(self.data, dict):
                self.data = {}
            if not isinstance(self.data.get("threads"), dict):
                self.data["threads"] = {}
            if not isinstance(self.data.get("settings"), dict):
                self.data["settings"] = {}
            data_changed = False
            settings = self.data["settings"]
            if "auto_reset_last_ym" not in settings:
                settings["auto_reset_last_ym"] = ""
                data_changed = True

            # Rebuild clan->thread mapping from persisted registrations.
            for config in self.clan_configs.values():
                config["thread_id"] = None

            threads = self.data["threads"]
            stale_thread_ids: Set[str] = set()
            for raw_thread_id, thread_data in list(threads.items()):
                thread_id = str(raw_thread_id)
                if not isinstance(thread_data, dict):
                    stale_thread_ids.add(thread_id)
                    data_changed = True
                    continue
                raw_stale_sticky_ids = thread_data.get("stale_sticky_message_ids")
                if raw_stale_sticky_ids is not None:
                    if not isinstance(raw_stale_sticky_ids, list):
                        thread_data.pop("stale_sticky_message_ids", None)
                        data_changed = True
                    else:
                        sticky_message_id = thread_data.get("sticky_message_id")
                        normalized_stale_sticky_ids = []
                        seen_stale_sticky_ids = set()
                        for raw_stale_sticky_id in raw_stale_sticky_ids:
                            try:
                                stale_sticky_id = int(raw_stale_sticky_id)
                            except (TypeError, ValueError):
                                data_changed = True
                                continue
                            if stale_sticky_id == sticky_message_id or stale_sticky_id in seen_stale_sticky_ids:
                                data_changed = True
                                continue
                            seen_stale_sticky_ids.add(stale_sticky_id)
                            normalized_stale_sticky_ids.append(stale_sticky_id)
                        if normalized_stale_sticky_ids:
                            if normalized_stale_sticky_ids != raw_stale_sticky_ids:
                                thread_data["stale_sticky_message_ids"] = normalized_stale_sticky_ids
                                data_changed = True
                        else:
                            thread_data.pop("stale_sticky_message_ids", None)
                            data_changed = True

                clan_name = thread_data.get("clan_name")
                if clan_name not in self.clan_configs:
                    stale_thread_ids.add(thread_id)
                    data_changed = True
                    continue

                try:
                    thread_id_int = int(thread_id)
                except (TypeError, ValueError):
                    stale_thread_ids.add(thread_id)
                    data_changed = True
                    continue

                existing_thread_id = self.clan_configs[clan_name].get("thread_id")
                if existing_thread_id is not None and existing_thread_id != thread_id_int:
                    existing_key = str(existing_thread_id)
                    existing_data = threads.get(existing_key, {})
                    existing_ts = None
                    if isinstance(existing_data, dict):
                        existing_ts = self._parse_iso_timestamp(existing_data.get("sticky_last_updated"))
                    current_ts = self._parse_iso_timestamp(thread_data.get("sticky_last_updated"))
                    keep_current = bool(current_ts and (not existing_ts or current_ts >= existing_ts))
                    if keep_current:
                        stale_thread_ids.add(existing_key)
                        self.clan_configs[clan_name]["thread_id"] = thread_id_int
                    else:
                        stale_thread_ids.add(thread_id)
                    data_changed = True
                    continue

                self.clan_configs[clan_name]["thread_id"] = thread_id_int

            for stale_id in stale_thread_ids:
                threads.pop(stale_id, None)

            if data_changed:
                self.save_data()
        except (OSError, json.JSONDecodeError, TypeError) as e:
            LOGGER.exception("Failed to load CWL thread data: %s", e)
            self.data = {"threads": {}, "settings": {}}


    def save_data(self) -> None:
        """Save CWL thread data to file."""
        try:
            write_json_atomic(self.data_file, self.data, indent=2, ensure_ascii=False)
        except (OSError, TypeError) as e:
            LOGGER.exception("Failed to save CWL thread data: %s", e)


    def _parse_iso_timestamp(self, value: Optional[str]) -> Optional[datetime]:
        """Parse ISO timestamps safely and normalize to UTC."""
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=dt_timezone.utc)
        return parsed


    def _current_auto_reset_month(self) -> Optional[str]:
        now = self._utc_now()
        if now.day < 13:
            return None
        return now.strftime("%Y-%m")


    @staticmethod
    def _first_day_of_next_month(current_date: date) -> date:
        if current_date.month == 12:
            return date(current_date.year + 1, 1, 1)
        return date(current_date.year, current_date.month + 1, 1)


    def _should_keep_registered_cwl_threads_open(self) -> bool:
        today = self._utc_now().date()
        if 1 <= today.day <= 12:
            return True
        days_until_next_month = (self._first_day_of_next_month(today) - today).days
        return 1 <= days_until_next_month <= 2


    def _get_sticky_lock(self, thread_id: str) -> asyncio.Lock:
        """Return a per-thread lock to prevent overlapping sticky updates."""
        lock = self._sticky_update_locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._sticky_update_locks[thread_id] = lock
        return lock


    def _drop_thread_registration(self, thread_id: str) -> None:
        """Remove a thread registration and associated in-memory trackers."""
        thread_key = str(thread_id)
        self.data.get("threads", {}).pop(thread_key, None)
        self.last_message_times.pop(thread_key, None)
        self.conversation_active.pop(thread_key, None)
        self.sticky_repositioned.pop(thread_key, None)
        self._sticky_update_locks.pop(thread_key, None)
