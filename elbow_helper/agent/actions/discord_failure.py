"""What a failed Discord request means, read once for lookups and confirmed changes."""

from __future__ import annotations

import traceback
from dataclasses import dataclass

import aiohttp
import discord


RATE_LIMITED = "Discord limited the bot's requests. Try again shortly."


@dataclass(frozen=True)
class DiscordFailure:
    kind: str  # not_found, refused, rate_limited or no_answer
    status: int | None
    text: str
    # A change that failed this way may still have been applied.
    uncertain: bool


def _raised_in_discord(error: BaseException) -> bool:
    """A connection error or timeout counts as Discord's only when discord.py raised it."""
    return any(str(frame.f_globals.get("__name__", "")).split(".")[0] == "discord"
               for frame, _ in traceback.walk_tb(error.__traceback__))


def discord_failure(error: BaseException) -> DiscordFailure | None:
    """Name a failure that came from Discord, or None for anything else."""
    if isinstance(error, discord.RateLimited):
        return DiscordFailure("rate_limited", 429, RATE_LIMITED, False)
    if isinstance(error, discord.HTTPException):
        status = error.status
        if status == 429:
            return DiscordFailure("rate_limited", status, RATE_LIMITED, False)
        if status == 404:
            return DiscordFailure("not_found", status, f"Discord couldn't find it (status {status}).", False)
        if 400 <= status < 500:
            return DiscordFailure("refused", status, f"Discord refused the request (status {status}).", False)
        return DiscordFailure("no_answer", status, f"Discord didn't answer the request (status {status}).", True)
    if isinstance(error, discord.ConnectionClosed) or (
            isinstance(error, (aiohttp.ClientConnectionError, TimeoutError)) and _raised_in_discord(error)):
        return DiscordFailure("no_answer", None, "Discord didn't answer the request.", True)
    return None
