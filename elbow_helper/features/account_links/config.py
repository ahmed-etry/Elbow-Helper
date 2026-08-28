"""Clan-linking configuration."""

from __future__ import annotations

from pathlib import Path

from elbow_helper.configuration.channels import REC_ROOM
from elbow_helper.configuration.clans import CLANS

COC_HTTP_TOTAL_TIMEOUT_SECONDS = 20

DB_PATH = Path("data/clan_links/links.sqlite3")
POLL_INTERVAL_MINUTES = 15
REVIEW_CHANNEL_ID = REC_ROOM

TRACKED_CLAN_CODES = tuple(CLANS.keys())
CLAN_FETCH_RETRIES = 3
CLAN_FETCH_BACKOFF_SECONDS = 1.5
CLAN_FETCH_CONCURRENCY = 3
CLAN_FETCH_WARNING_COOLDOWN_SECONDS = 300.0
LINK_ROLE_AT_OR_ABOVE_ELDER = frozenset({"admin", "coLeader", "leader"})
