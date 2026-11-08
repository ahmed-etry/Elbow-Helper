"""Plain-text views of Discord message content."""

from __future__ import annotations

import re


def normalize_message(content: str) -> str:
    """Lowercase message text with mentions, channel links and spoilers removed and spaces collapsed."""

    content = re.sub(r"<@!?\d+>", "", content)
    content = re.sub(r"<@&\d+>", "", content)
    content = re.sub(r"<#[0-9]+>", "", content)
    content = re.sub(r"\|\|.*?\|\|", "", content)
    return re.sub(r"\s+", " ", content).strip().lower()
