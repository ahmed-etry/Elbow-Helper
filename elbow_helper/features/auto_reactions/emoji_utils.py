"""Emoji parsing helpers for auto reactions."""

from __future__ import annotations

import re
from collections.abc import Iterable
from collections.abc import Sequence

CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")


def is_custom_emoji(token: str) -> bool:
    return bool(CUSTOM_EMOJI_RE.fullmatch(token))


try:
    import emoji as emoji_lib

    def _unicode_matches(text: str) -> list[tuple[int, str]]:
        matches: list[tuple[int, str]] = []
        for match in emoji_lib.emoji_list(text):
            emoji_token = match.get("emoji")
            start = match.get("match_start")
            if isinstance(emoji_token, str) and isinstance(start, int):
                matches.append((start, emoji_token))
        return matches
except ImportError:
    UNICODE_EMOJI_RE = re.compile(
        r"[\U0001F1E6-\U0001F1FF]|"
        r"[\U0001F300-\U0001F6FF]|"
        r"[\U0001F700-\U0001F77F]|"
        r"[\U00002600-\U000026FF]"
    )

    def _unicode_matches(text: str) -> list[tuple[int, str]]:
        return [(match.start(), match.group(0)) for match in UNICODE_EMOJI_RE.finditer(text)]


def extract_reaction_emojis(text: str) -> list[str]:
    matches = [(match.start(), match.group(0)) for match in CUSTOM_EMOJI_RE.finditer(text)]
    matches.extend(_unicode_matches(text))
    matches.sort(key=lambda item: item[0])

    found: list[str] = []
    seen: set[str] = set()
    for _, emoji_token in matches:
        if emoji_token in seen:
            continue
        seen.add(emoji_token)
        found.append(emoji_token)
    return found


def filter_reaction_emojis(emojis: Sequence[str], *, excluded: Iterable[str]) -> list[str]:
    excluded_tokens = set(excluded)
    return [emoji_token for emoji_token in emojis if emoji_token not in excluded_tokens]


def prioritize_reaction_emojis(emojis: Sequence[str], *, limit: int) -> list[str]:
    custom_tokens = [emoji_token for emoji_token in emojis if is_custom_emoji(emoji_token)]
    unicode_tokens = [emoji_token for emoji_token in emojis if not is_custom_emoji(emoji_token)]
    ordered = custom_tokens + unicode_tokens
    if limit <= 0:
        return ordered
    return ordered[:limit]
