"""Application emoji mapping for the live clan-war board."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from types import MappingProxyType
from typing import Mapping

import discord

from elbow_helper.discord.application_emojis import ApplicationEmojiCatalog
from elbow_helper.discord.application_emojis import get_application_emoji_provider


LOGGER = logging.getLogger(__name__)
TOWN_HALL_LEVELS = tuple(range(1, 19))
WAR_NUMBER_RANGE = tuple(range(0, 51))
WAR_EMOJI_NAMES = {
    "yellow_star": "war_yellow_star",
    "fire": "war_fire",
    "sword": "war_sword",
}


def required_war_emoji_names() -> tuple[str, ...]:
    return (
        *WAR_EMOJI_NAMES.values(),
        *(f"th{level}" for level in TOWN_HALL_LEVELS),
        *(f"war_number_{number}" for number in WAR_NUMBER_RANGE),
    )


@dataclass(frozen=True)
class WarEmojiSet:
    icons: Mapping[str, str]
    town_halls: Mapping[int, str]
    numbers: Mapping[int, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "icons", MappingProxyType(dict(self.icons)))
        object.__setattr__(self, "town_halls", MappingProxyType(dict(self.town_halls)))
        object.__setattr__(self, "numbers", MappingProxyType(dict(self.numbers)))

    @property
    def is_complete(self) -> bool:
        return (
            all(name in self.icons for name in WAR_EMOJI_NAMES)
            and all(level in self.town_halls for level in TOWN_HALL_LEVELS)
            and all(number in self.numbers for number in WAR_NUMBER_RANGE)
        )

    def icon(self, name: str, fallback: str) -> str:
        return self.icons.get(name, fallback)

    def town_hall(self, level: int) -> str:
        return self.town_halls.get(level, f"TH{level}")

    def number(self, number: int) -> str:
        return self.numbers.get(number, str(number))


EMPTY_WAR_EMOJIS = WarEmojiSet({}, {}, {})


class WarEmojiProvider:
    def __init__(self, client: discord.Client):
        self._provider = get_application_emoji_provider(client)
        self._current = EMPTY_WAR_EMOJIS
        self._last_missing: tuple[str, ...] | None = None

    async def get(self) -> WarEmojiSet:
        required = required_war_emoji_names()
        catalog = await self._provider.get(required_names=required)
        self._resolve(catalog)
        return self._current

    def _resolve(self, catalog: ApplicationEmojiCatalog) -> None:
        self._current = WarEmojiSet(
            icons={
                key: token
                for key, name in WAR_EMOJI_NAMES.items()
                if (token := catalog.get(name)) is not None
            },
            town_halls={
                level: token
                for level in TOWN_HALL_LEVELS
                if (token := catalog.get(f"th{level}")) is not None
            },
            numbers={
                number: token
                for number in WAR_NUMBER_RANGE
                if (token := catalog.get(f"war_number_{number}")) is not None
            },
        )
        missing = catalog.missing(required_war_emoji_names())
        if missing != self._last_missing:
            self._last_missing = missing
            if missing:
                LOGGER.warning(
                    "War application emojis are missing (%s): %s",
                    len(missing),
                    ", ".join(missing),
                )
