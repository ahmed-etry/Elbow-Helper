"""Cached discovery of emojis owned by the Discord application."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from types import MappingProxyType
from typing import Iterable, Mapping
import time
from weakref import WeakKeyDictionary

import discord

from elbow_helper.configuration.emojis import APPLICATION_EMOJI_REFRESH_SECONDS
from elbow_helper.configuration.emojis import APPLICATION_EMOJI_RETRY_SECONDS


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApplicationEmojiCatalog:
    tokens: Mapping[str, str]

    def __post_init__(self) -> None:
        normalized = {
            str(name).casefold(): str(token)
            for name, token in self.tokens.items()
        }
        object.__setattr__(self, "tokens", MappingProxyType(normalized))

    def get(self, name: str) -> str | None:
        return self.tokens.get(name.casefold())

    def missing(self, names: Iterable[str]) -> tuple[str, ...]:
        return tuple(name for name in names if self.get(name) is None)


class ApplicationEmojiProvider:
    """Fetch application emojis by name and retain the last usable catalog."""

    def __init__(self, client: discord.Client):
        self.client = client
        self._current = ApplicationEmojiCatalog({})
        self._last_attempt = 0.0
        self._last_success = 0.0
        self._lock = asyncio.Lock()

    @property
    def current(self) -> ApplicationEmojiCatalog:
        return self._current

    def _refresh_due(self, now: float, required_names: Iterable[str]) -> bool:
        if self._last_attempt == 0.0:
            return True
        if self._last_success == 0.0 or self._current.missing(required_names):
            return now - self._last_attempt >= APPLICATION_EMOJI_RETRY_SECONDS
        if now - self._last_success < APPLICATION_EMOJI_REFRESH_SECONDS:
            return False
        return now - self._last_attempt >= APPLICATION_EMOJI_RETRY_SECONDS

    async def get(
        self,
        *,
        required_names: Iterable[str] = (),
    ) -> ApplicationEmojiCatalog:
        names = tuple(required_names)
        if self._refresh_due(time.monotonic(), names):
            await self.refresh(required_names=names)
        return self._current

    async def refresh(
        self,
        *,
        required_names: Iterable[str] = (),
        force: bool = False,
    ) -> ApplicationEmojiCatalog:
        names = tuple(required_names)
        async with self._lock:
            now = time.monotonic()
            if not force and not self._refresh_due(now, names):
                return self._current
            self._last_attempt = now
            try:
                emojis = await self.client.fetch_application_emojis()
            except discord.HTTPException:
                LOGGER.warning("Application emojis could not be loaded", exc_info=True)
                return self._current

            self._current = ApplicationEmojiCatalog(
                {
                    str(emoji.name): str(emoji)
                    for emoji in emojis
                    if emoji.name
                }
            )
            self._last_success = time.monotonic()
            return self._current


_PROVIDERS: WeakKeyDictionary[discord.Client, ApplicationEmojiProvider] = (
    WeakKeyDictionary()
)


def get_application_emoji_provider(client: discord.Client) -> ApplicationEmojiProvider:
    provider = _PROVIDERS.get(client)
    if provider is None:
        provider = ApplicationEmojiProvider(client)
        _PROVIDERS[client] = provider
    return provider
