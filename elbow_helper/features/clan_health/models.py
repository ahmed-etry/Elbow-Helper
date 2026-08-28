"""Stable Clan Health verdict values stored and presented by the feature."""

from __future__ import annotations

from typing import Any

GOOD = "Good"
WATCH = "Watch"
NEEDS_REVIEW = "Needs Review"
INSUFFICIENT_DATA = "Insufficient data"
NOT_TRACKED = "Not tracked"
CLAN_HEALTHY = "Healthy"


def normalize_player_verdict(verdict: Any) -> str:
    text = str(verdict or "").strip()
    return text or INSUFFICIENT_DATA
