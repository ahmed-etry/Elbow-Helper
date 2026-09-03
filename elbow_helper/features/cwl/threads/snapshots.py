"""Selection and normalization for live CWL thread status boards."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from elbow_helper.features.wars.rendering import normalize_war_state

from ..helpers import coc_time_to_dt


@dataclass(frozen=True)
class CwlThreadRound:
    round_number: int
    war_tag: str
    season: str | None
    clan_name: str
    clan_tag: str
    clan_badge_url: str | None
    opponent_name: str
    opponent_tag: str
    state: str
    start_at: datetime | None
    end_at: datetime | None
    attacks_used: int
    attacks_total: int
    missing_attacks: tuple[str, ...]
    is_stale: bool


@dataclass(frozen=True)
class CwlThreadSnapshot:
    battle: CwlThreadRound | None
    preparation: CwlThreadRound | None

    @property
    def has_active_round(self) -> bool:
        return self.battle is not None or self.preparation is not None


def _positive_int(value: object) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _runtime_state(war: dict[str, Any], now: datetime) -> str:
    state = normalize_war_state(war.get("_state") or war.get("state"))
    start_at = coc_time_to_dt(war.get("startTime"))
    if state == "inwar" and start_at is not None and start_at > now:
        return "preparation"
    return state


def cwl_thread_snapshot_is_complete(
    wars: list[dict[str, Any]],
    *,
    now: datetime,
) -> bool:
    """Reject partial overlap snapshots before they hide useful board sections."""
    if not wars:
        return False

    total_rounds = max(
        (_positive_int(war.get("_total_rounds")) for war in wars),
        default=0,
    )
    if total_rounds <= 0:
        return False

    battle_rounds = {
        _positive_int(war.get("_round"))
        for war in wars
        if _runtime_state(war, now) == "inwar"
    }
    preparation_rounds = {
        _positive_int(war.get("_round"))
        for war in wars
        if _runtime_state(war, now) == "preparation"
    }
    ended_rounds = {
        _positive_int(war.get("_round"))
        for war in wars
        if _runtime_state(war, now) == "warended"
    }
    if battle_rounds:
        battle_round = max(battle_rounds)
        if battle_round < total_rounds and battle_round + 1 not in preparation_rounds:
            return False
    if preparation_rounds:
        preparation_round = min(preparation_rounds)
        previous_round = preparation_round - 1
        if (
            preparation_round > 1
            and previous_round not in battle_rounds
            and previous_round not in ended_rounds
        ):
            return False
    if not battle_rounds and not preparation_rounds:
        if not ended_rounds or max(ended_rounds) < total_rounds:
            return False
    return True


def _oriented_sides(
    war: dict[str, Any],
    clan_tag: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    clan = war.get("clan")
    opponent = war.get("opponent")
    if not isinstance(clan, dict) or not isinstance(opponent, dict):
        return None
    if clan.get("tag") == clan_tag:
        return clan, opponent
    if opponent.get("tag") == clan_tag:
        return opponent, clan
    return None


def _badge_url(clan: dict[str, Any]) -> str | None:
    badges = clan.get("badgeUrls") or {}
    if not isinstance(badges, dict):
        return None
    for size in ("small", "medium", "large"):
        candidate = badges.get(size)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _round_from_war(
    war: dict[str, Any],
    clan_tag: str,
    *,
    state: str,
    now: datetime,
) -> CwlThreadRound | None:
    sides = _oriented_sides(war, clan_tag)
    if sides is None:
        return None
    clan, opponent = sides
    members = clan.get("members") or []
    attacks_per_member = _positive_int(
        war.get("attacksPerMember") or clan.get("attacksPerMember") or 1
    ) or 1
    attacks_total = (
        _positive_int(war.get("teamSize")) or len(members)
    ) * attacks_per_member
    attacks_used = _positive_int(clan.get("attacks"))
    if not attacks_used:
        attacks_used = sum(
            len(member.get("attacks") or [])
            for member in members
            if isinstance(member, dict)
        )

    end_at = coc_time_to_dt(war.get("endTime"))
    show_missing = bool(
        state == "inwar"
        and end_at is not None
        and 0 <= (end_at - now).total_seconds() <= 2 * 3600
    )
    missing_attacks = ()
    if show_missing:
        missing_attacks = tuple(
            str(member.get("name") or "Unknown")
            for member in members
            if isinstance(member, dict)
            and len(member.get("attacks") or []) < attacks_per_member
        )

    raw_war_tag = str(war.get("_warTag") or "")
    raw_season = war.get("_season")
    return CwlThreadRound(
        round_number=_positive_int(war.get("_round")),
        war_tag=raw_war_tag,
        season=str(raw_season) if raw_season else None,
        clan_name=str(clan.get("name") or "Clan"),
        clan_tag=str(clan.get("tag") or clan_tag),
        clan_badge_url=_badge_url(clan),
        opponent_name=str(opponent.get("name") or "Unknown"),
        opponent_tag=str(opponent.get("tag") or ""),
        state=state,
        start_at=coc_time_to_dt(war.get("startTime")),
        end_at=end_at,
        attacks_used=attacks_used,
        attacks_total=attacks_total,
        missing_attacks=missing_attacks,
        is_stale=bool(war.get("_snapshot_stale")),
    )


def build_cwl_thread_snapshot(
    wars: list[dict[str, Any]],
    clan_tag: str,
    *,
    now: datetime,
) -> CwlThreadSnapshot:
    battle_candidates: list[CwlThreadRound] = []
    preparation_candidates: list[CwlThreadRound] = []
    for war in wars:
        state = _runtime_state(war, now)
        if state not in {"inwar", "preparation"}:
            continue
        normalized = _round_from_war(
            war,
            clan_tag,
            state=state,
            now=now,
        )
        if normalized is None:
            continue
        if state == "inwar":
            battle_candidates.append(normalized)
        else:
            preparation_candidates.append(normalized)

    battle = (
        max(battle_candidates, key=lambda item: item.round_number)
        if battle_candidates
        else None
    )
    future_preparations = [
        item
        for item in preparation_candidates
        if item.start_at is not None and item.start_at > now
    ]
    preparation = None
    if future_preparations:
        preparation = min(
            future_preparations,
            key=lambda item: (item.start_at, item.round_number),
        )
    elif preparation_candidates:
        preparation = max(
            preparation_candidates,
            key=lambda item: item.round_number,
        )
    return CwlThreadSnapshot(battle=battle, preparation=preparation)
