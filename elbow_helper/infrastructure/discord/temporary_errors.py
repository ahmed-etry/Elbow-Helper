"""Tell a temporary Discord or network failure from one that will keep failing."""

from __future__ import annotations

import asyncio

import aiohttp
import discord

_TEMPORARY_STATUSES = frozenset({429, 500, 502, 503, 504})
_TEMPORARY_MARKERS = (
    "temporary failure in name resolution",
    "name or service not known",
    "cannot connect to host",
    "connection reset",
    "server disconnected",
    "timed out",
    "timeout",
)


def is_temporary_discord_error(error: Exception) -> bool:
    """True for timeouts, dropped connections and Discord rate-limit or server errors."""

    if isinstance(error, (asyncio.TimeoutError, OSError, aiohttp.ClientConnectionError)):
        return True
    if isinstance(error, discord.HTTPException) and getattr(error, "status", None) in _TEMPORARY_STATUSES:
        return True
    text = str(error).lower()
    return any(marker in text for marker in _TEMPORARY_MARKERS)
