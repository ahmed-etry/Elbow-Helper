"""Nickname normalization and candidate matching for player links."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

import discord


_SEPARATOR_RE = re.compile(r"[\/|,\-]+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_SUFFIX_RE = re.compile(r"(?:v\d+|alt|mini|th\d+)$")


def _ascii_fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def normalize_name(value: str) -> str:
    folded = _ascii_fold(str(value or "").casefold())
    compact = _NON_ALNUM_RE.sub("", folded)
    return _SUFFIX_RE.sub("", compact)


def split_candidate_names(value: str) -> list[str]:
    raw = str(value or "")
    parts = [segment.strip() for segment in _SEPARATOR_RE.split(raw) if segment.strip()]
    if not parts:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        norm = normalize_name(part)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


@dataclass(frozen=True)
class MatchCandidate:
    member: discord.Member
    score: int
    reason: str


def _candidate_names(member: discord.Member) -> set[str]:
    values = {normalize_name(member.display_name), normalize_name(member.name)}
    if member.nick:
        values.update(split_candidate_names(member.nick))
    values.update(split_candidate_names(member.display_name))
    return {value for value in values if value}


def find_best_candidate(
    *,
    player_name: str,
    members: Iterable[discord.Member],
) -> MatchCandidate | None:
    target = normalize_name(player_name)
    if not target or len(target) < 3:
        return None

    candidates: list[MatchCandidate] = []
    for member in members:
        names = _candidate_names(member)
        if not names:
            continue
        if target in names:
            candidates.append(MatchCandidate(member=member, score=100, reason="exact normalized match"))
            continue
        if any(target == piece for piece in split_candidate_names(member.display_name)):
            candidates.append(MatchCandidate(member=member, score=95, reason="matched nickname segment"))
            continue
        if any(target in name or name in target for name in names if len(name) >= 3):
            candidates.append(MatchCandidate(member=member, score=70, reason="partial normalized match"))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (-item.score, item.member.display_name.casefold(), item.member.id))
    best = candidates[0]
    if best.score < 90:
        return None
    if len(candidates) > 1 and candidates[1].score == best.score:
        return None
    return best
