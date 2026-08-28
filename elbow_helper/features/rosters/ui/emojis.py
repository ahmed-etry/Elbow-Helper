"""Application-owned emoji discovery for roster Town Hall icons."""

from __future__ import annotations

from dataclasses import dataclass
import logging

import discord

from elbow_helper.discord.application_emojis import ApplicationEmojiCatalog
from elbow_helper.discord.application_emojis import get_application_emoji_provider


LOGGER = logging.getLogger(__name__)
TOWN_HALL_HEADER_EMOJI_NAME = "town_hall"
TOWN_HALL_LEVELS = tuple(range(1, 19))


@dataclass(frozen=True)
class TownHallEmojiSet:
    header: str | None
    levels: dict[int, str]

    @property
    def is_complete(self) -> bool:
        return self.header is not None and all(
            level in self.levels for level in TOWN_HALL_LEVELS
        )


class TownHallEmojiProvider:
    """Resolve application emojis by name and retain safe text fallbacks."""

    def __init__(self, client: discord.Client):
        self._provider = get_application_emoji_provider(client)
        self._current = TownHallEmojiSet(header=None, levels={})
        self._last_missing: tuple[str, ...] | None = None

    @property
    def current(self) -> TownHallEmojiSet:
        return self._current

    @staticmethod
    def _required_names() -> tuple[str, ...]:
        return (
            TOWN_HALL_HEADER_EMOJI_NAME,
            *(f"th{level}" for level in TOWN_HALL_LEVELS),
        )

    async def get(self) -> TownHallEmojiSet:
        catalog = await self._provider.get(required_names=self._required_names())
        self._resolve(catalog)
        return self._current

    async def refresh(self, *, force: bool = False) -> TownHallEmojiSet:
        catalog = await self._provider.refresh(
            required_names=self._required_names(),
            force=force,
        )
        self._resolve(catalog)
        return self._current

    def _resolve(self, catalog: ApplicationEmojiCatalog) -> None:
        resolved = TownHallEmojiSet(
            header=catalog.get(TOWN_HALL_HEADER_EMOJI_NAME),
            levels={
                level: token
                for level in TOWN_HALL_LEVELS
                if (token := catalog.get(f"th{level}")) is not None
            },
        )
        if resolved != self._current:
            self._current = resolved
        missing = catalog.missing(self._required_names())
        if missing != self._last_missing:
            self._last_missing = missing
            if missing:
                LOGGER.warning(
                    "Town Hall application emojis are missing: %s",
                    ", ".join(missing),
                )
