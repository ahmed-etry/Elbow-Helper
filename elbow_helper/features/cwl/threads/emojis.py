"""Application emoji mapping for CWL thread status boards."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

import discord

from elbow_helper.discord.application_emojis import ApplicationEmojiCatalog
from elbow_helper.discord.application_emojis import get_application_emoji_provider


CWL_THREAD_EMOJI_NAMES = {
    "war": "WAR",
    "clock": "Clock",
    "empty_sword": "EmptySword",
    "clan_castle": "CC",
    "filled": "GreenTick",
    "empty": "RedCross",
}


@dataclass(frozen=True)
class CwlThreadEmojiSet:
    icons: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "icons", MappingProxyType(dict(self.icons)))

    def icon(self, name: str, fallback: str) -> str:
        return self.icons.get(name, fallback)


EMPTY_CWL_THREAD_EMOJIS = CwlThreadEmojiSet({})


class CwlThreadEmojiProvider:
    """Resolve the Clash-themed application emojis used by the status board."""

    def __init__(self, client: discord.Client):
        self._provider = get_application_emoji_provider(client)
        self._current = EMPTY_CWL_THREAD_EMOJIS

    async def get(self) -> CwlThreadEmojiSet:
        catalog = await self._provider.get(
            required_names=CWL_THREAD_EMOJI_NAMES.values(),
        )
        self._resolve(catalog)
        return self._current

    def _resolve(self, catalog: ApplicationEmojiCatalog) -> None:
        self._current = CwlThreadEmojiSet(
            {
                key: token
                for key, emoji_name in CWL_THREAD_EMOJI_NAMES.items()
                if (token := catalog.get(emoji_name)) is not None
            }
        )
