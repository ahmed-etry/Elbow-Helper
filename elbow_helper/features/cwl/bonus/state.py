"""Persistent state ownership for CWL bonus dashboards."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from elbow_helper.infrastructure.persistence import read_json
from elbow_helper.infrastructure.persistence import write_json_atomic

from ..config import BONUS_DASHBOARD_STATE_FILE


LOGGER = logging.getLogger(__name__)


class BonusDashboardStore:
    """Own dashboard JSON state and per-board concurrency locks."""

    def __init__(
        self,
        path: Path = BONUS_DASHBOARD_STATE_FILE,
    ):
        self._path = path
        self._locks: dict[str, asyncio.Lock] = {}
        self.state = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            raw = read_json(self._path) if self._path.exists() else {}
        except (OSError, json.JSONDecodeError, TypeError) as error:
            LOGGER.warning(
                "Failed to load bonus dashboard state: %s",
                error,
            )
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        if not isinstance(raw.get("boards"), dict):
            raw["boards"] = {}
        return raw

    def save(self) -> None:
        try:
            write_json_atomic(
                self._path,
                self.state,
                indent=2,
                ensure_ascii=False,
            )
        except (OSError, TypeError) as error:
            LOGGER.warning(
                "Failed to save bonus dashboard state: %s",
                error,
            )

    def lock(self, board_key: str) -> asyncio.Lock:
        return self._locks.setdefault(board_key, asyncio.Lock())
