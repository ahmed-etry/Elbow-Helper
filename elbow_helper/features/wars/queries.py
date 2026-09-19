"""Typed read-only evidence from the regular-war board state."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import timezone
import math
from typing import Any, TYPE_CHECKING

from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import normalize_player_tag

from .config import WAR_BOARD_CLAN_CODES
from .helpers import build_war_id
from .rendering import coc_time_to_datetime, normalize_war_state

if TYPE_CHECKING:
    from .cog import WarManager


@dataclass(frozen=True, slots=True)
class WarMemberEvidence:
    player_tag: str
    player_name: str
    townhall: int
    map_position: int
    attacks_expected: int
    attacks_used: int
    attacks_remaining: int
    missed_attacks: int
    stars: int
    destruction: float


@dataclass(frozen=True, slots=True)
class RegularWarSnapshot:
    clan_code: str
    evidence_status: str
    observed_at: str | None
    selected: str
    war_id: str | None
    state: str | None
    preparation_start_at: str | None
    start_at: str | None
    end_at: str | None
    team_size: int
    attacks_per_member: int
    clan_tag: str | None
    clan_name: str | None
    clan_stars: int | None
    clan_destruction: float | None
    opponent_tag: str | None
    opponent_name: str | None
    opponent_stars: int | None
    opponent_destruction: float | None
    result: str | None
    roster_complete: bool
    members: tuple[WarMemberEvidence, ...]
    issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RegularWarStatus:
    clan_code: str
    evidence_status: str
    observed_at: str | None
    current_war_id: str | None
    current_state: str | None
    previous_war_id: str | None


class WarQueries:
    """Expose copied war evidence without polling Clash or mutating board state."""

    def __init__(self, manager: WarManager):
        self._manager = manager

    @property
    def supported_clan_codes(self) -> tuple[str, ...]:
        return tuple(WAR_BOARD_CLAN_CODES)

    def statuses(self) -> tuple[RegularWarStatus, ...]:
        return tuple(self.status(clan_code) for clan_code in WAR_BOARD_CLAN_CODES)

    def status(self, clan_code: str) -> RegularWarStatus:
        _require_clan(clan_code)
        history, observation = self._copied_state(clan_code)
        current = history.get("current")
        previous = history.get("previous")
        evidence_status, observed_at = _evidence_status(observation, current)
        current_state = normalize_war_state(current.get("state")) if isinstance(current, dict) else None
        if evidence_status in {"not_in_war", "cwl_active", "unavailable"}:
            current_state = None
            current_id = None
        else:
            current_id = build_war_id(current) if isinstance(current, dict) else None
        return RegularWarStatus(
            clan_code=clan_code, evidence_status=evidence_status,
            observed_at=observed_at, current_war_id=current_id,
            current_state=current_state,
            previous_war_id=build_war_id(previous) if isinstance(previous, dict) else None,
        )

    def snapshot(self, clan_code: str, *, selected: str = "current") -> RegularWarSnapshot:
        _require_clan(clan_code)
        if selected not in {"current", "previous"}:
            raise ValueError("War selection must be current or previous")
        history, observation = self._copied_state(clan_code)
        current = history.get("current")
        status, observed_at = _evidence_status(observation, current)
        if selected == "current":
            if status in {"not_in_war", "cwl_active", "unavailable", "observation_unknown"}:
                return _empty_snapshot(clan_code, status, observed_at, selected)
            payload = current
            evidence_status = status
        else:
            payload = history.get("previous")
            evidence_status = "stored_previous" if isinstance(payload, dict) else "unavailable"
            observed_at = None
        if not isinstance(payload, dict):
            return _empty_snapshot(clan_code, evidence_status, observed_at, selected)
        return _snapshot_from_payload(
            clan_code, payload, evidence_status=evidence_status,
            observed_at=observed_at, selected=selected,
        )

    def _copied_state(self, clan_code: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
        raw_history = getattr(self._manager, "war_board_history", {})
        raw_observations = getattr(self._manager, "war_observations", {})
        history = raw_history.get(clan_code, {}) if isinstance(raw_history, dict) else {}
        observation = raw_observations.get(clan_code) if isinstance(raw_observations, dict) else None
        return (
            deepcopy(history) if isinstance(history, dict) else {},
            deepcopy(observation) if isinstance(observation, dict) else None,
        )


def _evidence_status(
    observation: dict[str, Any] | None, current: Any,
) -> tuple[str, str | None]:
    if observation is not None:
        state = str(observation.get("state") or "")
        observed_at = observation.get("observed_at")
        if not isinstance(observed_at, str) or not observed_at:
            observed_at = None
        if state == "notinwar":
            return "not_in_war", observed_at
        if state == "cwl":
            return "cwl_active", observed_at
        if state in {"preparation", "inwar", "warended"}:
            observed_id = observation.get("war_id")
            if isinstance(current, dict) and observed_id == build_war_id(current):
                return "observed", observed_at
            return "observation_mismatch", observed_at
        return "observation_unknown", observed_at
    if isinstance(current, dict):
        return "cached_unverified", None
    return "unavailable", None


def _snapshot_from_payload(
    clan_code: str, payload: dict[str, Any], *, evidence_status: str,
    observed_at: str | None, selected: str,
) -> RegularWarSnapshot:
    state = normalize_war_state(payload.get("state"))
    if state not in {"preparation", "inwar", "warended"}:
        raise ValueError("Stored regular-war snapshot has an unsupported state")
    team_size = _bounded_int(payload.get("teamSize"), minimum=1, maximum=50)
    attacks_per_member = _bounded_int(
        payload.get("attacksPerMember", 2), minimum=1, maximum=2,
    )
    clan = payload.get("clan")
    opponent = payload.get("opponent")
    if not isinstance(clan, dict) or not isinstance(opponent, dict):
        raise ValueError("Stored regular-war snapshot is missing a side")
    raw_members = clan.get("members") or []
    if not isinstance(raw_members, list) or len(raw_members) > 50:
        raise ValueError("Stored regular-war roster is invalid")
    members = []
    issues = []
    seen_tags = set()
    for value in raw_members:
        if not isinstance(value, dict):
            raise ValueError("Stored regular-war member is invalid")
        tag = normalize_player_tag(str(value.get("tag") or ""))
        if not tag or tag in seen_tags:
            raise ValueError("Stored regular-war roster has an invalid account tag")
        seen_tags.add(tag)
        attacks = value.get("attacks") or []
        if not isinstance(attacks, list) or len(attacks) > attacks_per_member:
            raise ValueError("Stored regular-war attacks are invalid")
        stars = 0
        destruction = 0.0
        for attack in attacks:
            if not isinstance(attack, dict):
                raise ValueError("Stored regular-war attack is invalid")
            stars += _bounded_int(attack.get("stars", 0), minimum=0, maximum=3)
            destruction += _bounded_float(
                attack.get("destructionPercentage", 0), minimum=0, maximum=100,
            )
        used = len(attacks)
        remaining = attacks_per_member - used
        members.append(WarMemberEvidence(
            player_tag=tag, player_name=_member_name(value.get("name")),
            townhall=_bounded_int(value.get("townhallLevel", 0), minimum=0, maximum=20),
            map_position=_bounded_int(value.get("mapPosition", 0), minimum=0, maximum=50),
            attacks_expected=attacks_per_member, attacks_used=used,
            attacks_remaining=remaining,
            missed_attacks=remaining if state == "warended" else 0,
            stars=stars, destruction=destruction,
        ))
    members.sort(key=lambda row: (row.map_position or 51, row.player_name.casefold(), row.player_tag))
    roster_complete = len(members) == team_size
    if not roster_complete:
        issues.append(f"incomplete_roster:{len(members)}/{team_size}")
    clan_stars = _optional_int(clan.get("stars"), minimum=0)
    opponent_stars = _optional_int(opponent.get("stars"), minimum=0)
    clan_destruction = _optional_float(clan.get("destructionPercentage"), minimum=0, maximum=100)
    opponent_destruction = _optional_float(opponent.get("destructionPercentage"), minimum=0, maximum=100)
    result = None
    if state == "warended" and None not in (
        clan_stars, opponent_stars, clan_destruction, opponent_destruction,
    ):
        clan_score = (clan_stars, clan_destruction)
        opponent_score = (opponent_stars, opponent_destruction)
        result = "won" if clan_score > opponent_score else "lost" if clan_score < opponent_score else "tied"
    return RegularWarSnapshot(
        clan_code=clan_code, evidence_status=evidence_status,
        observed_at=observed_at, selected=selected, war_id=build_war_id(payload),
        state=state, preparation_start_at=_time(payload.get("preparationStartTime")),
        start_at=_time(payload.get("startTime")), end_at=_time(payload.get("endTime")),
        team_size=team_size, attacks_per_member=attacks_per_member,
        clan_tag=_optional_text(clan.get("tag")), clan_name=_optional_text(clan.get("name")),
        clan_stars=clan_stars, clan_destruction=clan_destruction,
        opponent_tag=_optional_text(opponent.get("tag")),
        opponent_name=_optional_text(opponent.get("name")),
        opponent_stars=opponent_stars, opponent_destruction=opponent_destruction,
        result=result, roster_complete=roster_complete, members=tuple(members),
        issues=tuple(issues),
    )


def _empty_snapshot(
    clan_code: str, status: str, observed_at: str | None, selected: str,
) -> RegularWarSnapshot:
    return RegularWarSnapshot(
        clan_code=clan_code, evidence_status=status, observed_at=observed_at,
        selected=selected, war_id=None, state=None, preparation_start_at=None,
        start_at=None, end_at=None, team_size=0, attacks_per_member=0,
        clan_tag=None, clan_name=None, clan_stars=None, clan_destruction=None,
        opponent_tag=None, opponent_name=None, opponent_stars=None,
        opponent_destruction=None, result=None, roster_complete=False,
        members=(), issues=(),
    )


def _require_clan(clan_code: str) -> None:
    if clan_code not in CLANS or clan_code not in WAR_BOARD_CLAN_CODES:
        raise ValueError("Unknown regular-war clan code")


def _bounded_int(value: Any, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Stored regular-war integer is out of bounds")
    return value


def _bounded_float(value: Any, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Stored regular-war number is invalid")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError("Stored regular-war number is out of bounds")
    return result


def _optional_int(value: Any, *, minimum: int) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < minimum:
        raise ValueError("Stored regular-war integer is invalid")
    return value


def _optional_float(
    value: Any, *, minimum: float, maximum: float,
) -> float | None:
    if value is None:
        return None
    return _bounded_float(value, minimum=minimum, maximum=maximum)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("Stored regular-war text is invalid")
    return value or None


def _member_name(value: Any) -> str:
    if value is None:
        return "Unknown"
    if not isinstance(value, str) or not value or len(value) > 100:
        raise ValueError("Stored regular-war member name is invalid")
    return value


def _time(value: Any) -> str | None:
    if value is None:
        return None
    parsed = coc_time_to_datetime(value)
    if parsed is None:
        raise ValueError("Stored regular-war timestamp is invalid")
    return parsed.astimezone(timezone.utc).isoformat()


__all__ = [
    "RegularWarSnapshot", "RegularWarStatus", "WarMemberEvidence", "WarQueries",
]
