"""TicketTool field parsing and exam ticket-type detection."""

from __future__ import annotations

import re
from typing import Dict, List, Optional

import discord

from elbow_helper.configuration.clans import CLAN_NAMES
from elbow_helper.configuration.clans import CLAN_ORDER

from ..availability import _clean_answer
from ..availability import _normalize_question
from ..availability import _normalize_text
from ..availability import _strip_invisible

TICKET_TRIGGERS = {
    "clan_promo": "will soon check with you regarding your clan promotion",
    "elder_promo": "will soon check with you regarding your elder promotion",
}

TICKET_KEYWORDS = {
    "clan_promo": ["clan promotion"],
    "elder_promo": ["elder promotion"],
}

TICKET_RENAME = {
    "clan_promo": {"emoji": "🏡", "short": "promotion"},
    "elder_promo": {"emoji": "🪬", "short": "promotion"},
}

QUESTION_MAP = {
    "what's your town hall level": "th_level",
    "whats your town hall level": "th_level",
    "what is your town hall level": "th_level",
    "why are you going for elder": "elder_reason",
    "which war should we review": "elder_war",
    "in which clan did you do your attacks": "elder_clan",
}


def normalize_message(content: str) -> str:
    content = re.sub(r"<@!?\d+>", "", content)
    content = re.sub(r"<@&\d+>", "", content)
    content = re.sub(r"<#[0-9]+>", "", content)
    content = re.sub(r"\|\|.*?\|\|", "", content)
    return re.sub(r"\s+", " ", content).strip().lower()


def infer_ticket_type(text: str) -> Optional[str]:
    norm = normalize_message(text)
    if not norm:
        return None
    for ticket_type, trigger in TICKET_TRIGGERS.items():
        if trigger in norm:
            return ticket_type
    for ticket_type, keywords in TICKET_KEYWORDS.items():
        if any(keyword in norm for keyword in keywords):
            return ticket_type
    if "promotion" in norm and "elder" in norm:
        return "elder_promo"
    if "promotion" in norm and "clan" in norm:
        return "clan_promo"
    return None


def extract_th_level(text: str) -> Optional[int]:
    if not text:
        return None
    match = re.search(r"\bth\s*([0-9]{1,2})\b", text, re.IGNORECASE)
    if not match:
        match = re.search(r"\btown\s*hall\s*([0-9]{1,2})\b", text, re.IGNORECASE)
    if not match:
        match = re.search(r"\b(1[0-9]|20)\b", text.strip())
    if not match:
        return None
    try:
        th = int(match.group(1))
    except ValueError:
        return None
    return th if 10 <= th <= 20 else None


def _normalize_clan_lookup_text(text: str) -> str:
    normalized = _normalize_text(text).lower()
    return re.sub(r"\s+", " ", normalized).strip()


def _build_clan_match_pattern(value: str) -> Optional[re.Pattern[str]]:
    normalized = _normalize_clan_lookup_text(value)
    tokens = re.findall(r"[a-z0-9]+", normalized)
    if not tokens:
        return None
    pattern = r"(?<!\w)" + r"(?:[\W_]+)".join(map(re.escape, tokens)) + r"(?!\w)"
    return re.compile(pattern)


_CLAN_MATCH_PATTERNS = {
    code: tuple(
        pattern
        for pattern in (
            _build_clan_match_pattern(code),
            _build_clan_match_pattern(CLAN_NAMES.get(code, "")),
        )
        if pattern is not None
    )
    for code in CLAN_ORDER
}


def extract_clan_codes(text: str) -> List[str]:
    if not text:
        return []
    normalized = _normalize_clan_lookup_text(text)
    if not normalized:
        return []
    matches: List[str] = []
    for code in CLAN_ORDER:
        patterns = _CLAN_MATCH_PATTERNS.get(code, ())
        if any(pattern.search(normalized) for pattern in patterns):
            matches.append(code)
    return matches


def _extract_ticket_lines(messages: List[discord.Message]) -> List[str]:
    lines: List[str] = []
    for msg in messages:
        if msg.content:
            lines.extend([_strip_invisible(line) for line in msg.content.splitlines() if line.strip()])
        for emb in msg.embeds:
            if emb.title:
                lines.append(_strip_invisible(emb.title))
            if emb.description:
                lines.extend([_strip_invisible(line) for line in emb.description.splitlines() if line.strip()])
            for field in emb.fields:
                if field.name:
                    lines.append(_strip_invisible(field.name))
                if field.value:
                    lines.extend([_strip_invisible(line) for line in field.value.splitlines() if line.strip()])
    return [line for line in lines if line]


def _parse_tickettool_description(description: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    if not description:
        return fields
    pattern = r"\*\*(.+?)\*\*\s*```(?:\n)?(.*?)```"
    for question, answer in re.findall(pattern, description, flags=re.DOTALL):
        norm = _normalize_question(question)
        key = None
        for question_text, mapped in QUESTION_MAP.items():
            if norm.startswith(question_text):
                key = mapped
                break
        if not key:
            continue
        cleaned = _clean_answer(answer)
        if cleaned:
            fields[key] = cleaned
    return fields


def parse_ticket_fields(lines: List[str]) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    pending_key = None
    for raw in lines:
        line = _normalize_text(raw)
        if not line:
            continue
        norm = _normalize_question(line)
        if pending_key is None:
            for question, key in QUESTION_MAP.items():
                if norm.startswith(question):
                    pending_key = key
                    if "?" in line:
                        remainder = line.split("?", 1)[1]
                        remainder = re.sub(r"^[\s:\-]+", "", remainder).strip()
                        remainder = _clean_answer(remainder)
                        if remainder:
                            fields[key] = remainder
                            pending_key = None
                    break
            if pending_key:
                continue
        if pending_key:
            fields[pending_key] = _clean_answer(line)
            pending_key = None
    return fields


__all__ = [
    "QUESTION_MAP",
    "TICKET_KEYWORDS",
    "TICKET_RENAME",
    "TICKET_TRIGGERS",
    "_extract_ticket_lines",
    "_parse_tickettool_description",
    "extract_clan_codes",
    "extract_th_level",
    "infer_ticket_type",
    "normalize_message",
    "parse_ticket_fields",
]
