"""Shared slash-command autocomplete lookups."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Sequence
from typing import Any

from discord import app_commands

PLAYER_CHOICE_LIMIT = 25
PLAYER_SEARCH_TIMEOUT_SECONDS = 1.5


async def player_choices(
    search_players: Callable[[str, int], Sequence[dict[str, Any]]],
    current: str,
    *,
    logger: logging.Logger | None = None,
    label: str = "Player autocomplete",
) -> list[app_commands.Choice[str]]:
    """Choices for a player-tag option, or none if the search is slow or fails.

    Pass `logger` to record a timeout or failure under `label`; without it they stay silent."""

    try:
        rows = await asyncio.wait_for(
            asyncio.to_thread(search_players, current, PLAYER_CHOICE_LIMIT),
            timeout=PLAYER_SEARCH_TIMEOUT_SECONDS,
        )
        return [
            app_commands.Choice(
                name=(
                    f"{row.get('player_name') or row['player_tag']} - "
                    f"{row.get('clan_code') or '-'} - "
                    f"TH{int(row.get('townhall') or 0)} - {row['player_tag']}"
                )[:100],
                value=str(row["player_tag"]),
            )
            for row in rows
        ]
    except TimeoutError:
        if logger is not None:
            logger.warning("%s timed out", label)
        return []
    except (KeyError, TypeError, ValueError, sqlite3.Error):
        if logger is not None:
            logger.exception("%s failed", label)
        return []
