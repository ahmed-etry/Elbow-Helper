"""Application emoji mapping for attack-plan unit icons."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from types import MappingProxyType
from typing import Iterable, Mapping

import discord

from elbow_helper.discord.application_emojis import ApplicationEmojiCatalog
from elbow_helper.discord.application_emojis import get_application_emoji_provider


LOGGER = logging.getLogger(__name__)

_EMOJI_NAME_OVERRIDES = {
    "Angry Spell": "angry_spell",
    "Electro Owl": "Owl",
    "Henchmen Puppet": "Henchmen",
    "Hog Rider Puppet": "HogPuppet",
    "Invisibility Spell": "Invisible",
    "Mighty Yak": "Yak",
    "Monolith Arrow": "monolith_arrow",
    "Revenge Deck": "revenge_deck",
    "Ruin Witch": "ruin_witch",
    "Spiky Ball": "SpikeyBall",
}
_NON_EMOJI_NAME_CHARACTERS = re.compile(r"[^A-Za-z0-9_]")


def application_emoji_name(unit_name: str) -> str:
    """Return the original ClashPerk emoji name for a Clash unit."""

    if unit_name in _EMOJI_NAME_OVERRIDES:
        return _EMOJI_NAME_OVERRIDES[unit_name]
    if unit_name.endswith(" Spell"):
        unit_name = unit_name.removesuffix(" Spell")
    return _NON_EMOJI_NAME_CHARACTERS.sub("", unit_name)


@dataclass(frozen=True)
class AttackPlanEmojiSet:
    tokens: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "tokens", MappingProxyType(dict(self.tokens)))

    def get(self, unit_name: str) -> str | None:
        return self.tokens.get(unit_name)


EMPTY_ATTACK_PLAN_EMOJIS = AttackPlanEmojiSet({})


class AttackPlanEmojiProvider:
    """Resolve attack-plan application emojis by their original names."""

    def __init__(self, client: discord.Client):
        self._provider = get_application_emoji_provider(client)
        self._current = EMPTY_ATTACK_PLAN_EMOJIS
        self._last_missing: tuple[str, ...] | None = None

    async def get(self, unit_names: Iterable[str]) -> AttackPlanEmojiSet:
        names = tuple(dict.fromkeys(unit_names))
        required = tuple(application_emoji_name(name) for name in names)
        catalog = await self._provider.get(required_names=required)
        self._resolve(catalog, names)
        return self._current

    def _resolve(
        self,
        catalog: ApplicationEmojiCatalog,
        unit_names: tuple[str, ...],
    ) -> None:
        self._current = AttackPlanEmojiSet(
            {
                unit_name: token
                for unit_name in unit_names
                if (
                    token := catalog.get(application_emoji_name(unit_name))
                ) is not None
            }
        )
        missing = tuple(
            application_emoji_name(unit_name)
            for unit_name in unit_names
            if self._current.get(unit_name) is None
        )
        if missing != self._last_missing:
            self._last_missing = missing
            if missing:
                LOGGER.warning(
                    "Attack-plan application emojis are missing (%s): %s",
                    len(missing),
                    ", ".join(missing),
                )
