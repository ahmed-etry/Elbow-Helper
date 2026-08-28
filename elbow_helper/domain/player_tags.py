"""Canonical Clash player-tag handling."""

from __future__ import annotations

from urllib.parse import quote


PLAYER_TAG_CHARACTERS = frozenset("0289PYLQGRJCUV")


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
