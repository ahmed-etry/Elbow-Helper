"""Clash API access for regular-war workflows."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from elbow_helper.domain.player_tags import encode_clash_tag

from .config import (
    CLAN_TAGS,
    WAR_FETCH_BACKOFF_SECONDS,
    WAR_FETCH_RETRIES,
    WAR_FETCH_WARNING_COOLDOWN_SECONDS,
)

LOGGER = logging.getLogger(__name__)


class ApiMixin:
    def _log_war_fetch_failure(self, clan: str, detail: str, *, transient: bool) -> None:
        now = time.monotonic()
        state = self._war_fetch_warning_state.get(clan)
        if state:
            last_detail = str(state.get("detail") or "")
            last_ts = float(state.get("last_ts") or 0.0)
            if detail == last_detail and (now - last_ts) < WAR_FETCH_WARNING_COOLDOWN_SECONDS:
                state["suppressed"] = int(state.get("suppressed") or 0) + 1
                self._war_fetch_warning_state[clan] = state
                return

        suppressed = int((state or {}).get("suppressed") or 0)
        suffix = f" (suppressed {suppressed} similar warnings)" if suppressed else ""
        log = LOGGER.info if transient else LOGGER.warning
        log("Failed to fetch current war for %s: %s%s", clan, detail, suffix)
        self._war_fetch_warning_state[clan] = {
            "detail": detail,
            "last_ts": now,
            "suppressed": 0,
        }

    async def _fetch_current_war(self, clan: str) -> Optional[Dict[str, Any]]:
        """Fetch the current regular-war state for one configured clan."""

        if not self.clash_client.configured:
            return None
        tag = CLAN_TAGS.get(clan)
        if not tag:
            return None

        response = await self.clash_client.get(
            f"/clans/{encode_clash_tag(tag)}/currentwar",
            attempts=WAR_FETCH_RETRIES,
            timeout_seconds=15,
            backoff_seconds=WAR_FETCH_BACKOFF_SECONDS,
        )
        if response.status == 404:
            self._war_fetch_warning_state.pop(clan, None)
            return {"state": "notInWar"}

        payload = response.payload_object
        if response.ok and payload is not None:
            self._war_fetch_warning_state.pop(clan, None)
            return payload

        detail = str(response.error or f"status={response.status}").strip()
        if response.attempts > 1:
            detail = f"{detail} (after {response.attempts} attempts)"
        self._log_war_fetch_failure(clan, detail, transient=response.transient)
        return None

    async def _fetch_war_log_result(
        self,
        clan: str,
        previous: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Recover final aggregates when a new war replaced the last live snapshot."""

        if not self.clash_client.configured:
            return None
        tag = CLAN_TAGS.get(clan)
        if not tag:
            return None
        expected_end = str(previous.get("endTime") or "")
        expected_opponent = str((previous.get("opponent") or {}).get("tag") or "")
        if not expected_end or not expected_opponent:
            return None

        response = await self.clash_client.get(
            f"/clans/{encode_clash_tag(tag)}/warlog?limit=10",
            attempts=WAR_FETCH_RETRIES,
            timeout_seconds=15,
            backoff_seconds=WAR_FETCH_BACKOFF_SECONDS,
        )
        if response.status in {403, 404}:
            return None

        payload = response.payload_object
        if response.ok and payload is not None:
            for item in payload.get("items", []) or []:
                if (
                    str(item.get("endTime") or "") == expected_end
                    and str((item.get("opponent") or {}).get("tag") or "")
                    == expected_opponent
                ):
                    return item
            return None

        detail = str(response.error or f"status={response.status}").strip()
        self._log_war_fetch_failure(
            f"{clan} war log",
            detail,
            transient=response.transient,
        )
        return None
