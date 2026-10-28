"""Reject report data from a different guild."""

from typing import Any


def require_guild(row: dict[str, Any], guild_id: int, label: str) -> None:
    if row["guild_id"] != guild_id:
        raise ValueError(f"{label} belongs to another guild")
