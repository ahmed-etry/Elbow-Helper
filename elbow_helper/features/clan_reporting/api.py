"""CoC API access for clan-reporting war data."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from elbow_helper.domain.player_tags import encode_clash_tag

LOGGER = logging.getLogger(__name__)


class ClanReportingApiMixin:
    """HTTP session and Clash of Clans API helpers."""

    async def _fetch_war_log(self, clan_tag: str, limit: int = 50) -> Optional[list[dict[str, Any]]]:
        if not self.clash_client.configured:
            return None

        response = await self.clash_client.get(
            f"/clans/{encode_clash_tag(clan_tag)}/warlog?limit={limit}",
            timeout_seconds=15,
        )
        if response.status == 403:
            return None
        payload = response.payload_object
        if not response.ok or payload is None:
            log = LOGGER.info if response.transient else LOGGER.warning
            detail = response.error or f"status={response.status}"
            log("Failed to fetch war log for %s: %s", clan_tag, detail)
            return None
        items = payload.get("items", [])
        return items if isinstance(items, list) else []

    def _parse_warlog_time(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None

        for fmt in ("%Y%m%dT%H%M%S.%fZ", "%Y%m%dT%H%M%S"):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None
