"""Canonical Clash player-tag handling."""

from __future__ import annotations

from urllib.parse import quote
import re


PLAYER_TAG_CHARACTERS = frozenset("0289PYLQGRJCUV")
_TAG_IN_TEXT = re.compile(
    r"(?<![\w#])#[" + "".join(sorted(PLAYER_TAG_CHARACTERS)) + r"]{2,15}(?!\w)", re.IGNORECASE,
)


def canonical_player_tag(value: object) -> str | None:
    """Return a consistently formatted tag without deciding whether it is valid."""

    tag = str(value or "").strip().upper().replace("O", "0")
    if not tag:
        return None
    if not tag.startswith("#"):
        tag = f"#{tag}"
    return tag


def normalize_player_tag(value: object) -> str | None:
    """Return a valid canonical player tag, or ``None`` for invalid input."""

    tag = canonical_player_tag(value)
    if tag is None:
        return None
    body = tag[1:]
    if not body or any(character not in PLAYER_TAG_CHARACTERS for character in body):
        return None
    return tag


def encode_clash_tag(value: object) -> str:
    """Encode a canonical Clash tag for use in an API path segment."""

    tag = canonical_player_tag(value)
    return quote(tag or "", safe="")


def find_player_tags(text: str) -> frozenset[str]:
    """Find distinct, valid canonical tags explicitly written in text."""
    return frozenset(tag for candidate in _TAG_IN_TEXT.findall(text)
                     if (tag := normalize_player_tag(candidate)) is not None)
