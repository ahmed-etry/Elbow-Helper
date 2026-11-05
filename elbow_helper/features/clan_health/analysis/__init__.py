"""Clan Health scoring and workbook analysis service."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..config import CLAN_PROFILE_BY_CODE
from ..config import PROFILE_NAMES
from ..record import HealthRecord
from ..record_wars import RegularWars
from .flags import ClanHealthFlagMixin
from .overlays import ClanHealthOverlayMixin
from .sheets import ClanHealthSheetMixin
from .verdicts import assess_war_participation


class ClanHealthAnalyzer(
    ClanHealthOverlayMixin,
    ClanHealthFlagMixin,
    ClanHealthSheetMixin,
):
    """Apply scoring policy to activity and wars from the family record, when a report is asked for."""

    def __init__(self, record: HealthRecord, regular_wars: RegularWars, league_wars: RegularWars):
        self.record = record
        self.regular_wars = regular_wars
        self.league_wars = league_wars

    @staticmethod
    def profile_for_clan(clan_code: str | None) -> str:
        code = str(clan_code or "").upper()
        profile = CLAN_PROFILE_BY_CODE.get(code, "casual")
        return profile if profile in PROFILE_NAMES else "casual"

    _profile_for_clan = profile_for_clan

    @staticmethod
    def assess_war_participation(
        *,
        profile_rules: dict[str, Any],
        cycle_start: datetime | None,
        cycle_end: datetime | None,
        war_events_joined: int,
        war_expected: int,
        war_used: int,
        available_wars_in_window: int | None = None,
    ) -> dict[str, Any]:
        result = assess_war_participation(
            profile_rules=profile_rules,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
            war_events_joined=war_events_joined,
            war_expected=war_expected,
            war_used=war_used,
            available_wars_in_window=available_wars_in_window,
        )
        result["reason"] = " | ".join(
            part
            for part in (
                result.get("attendance_reason"),
                result.get("hit_usage_reason"),
            )
            if part
        )
        return result

    _assess_war_participation = assess_war_participation

    apply_progression_fallback = (
        ClanHealthOverlayMixin._apply_progression_delta_fallback
    )
    apply_war_activity = ClanHealthOverlayMixin._apply_family_war_activity
    apply_raid_activity = ClanHealthOverlayMixin._apply_family_raid_activity
    apply_donation_activity = (
        ClanHealthOverlayMixin._apply_family_donation_activity
    )
    apply_flags = ClanHealthFlagMixin._apply_flags
    build_sheets = ClanHealthSheetMixin._build_sheets


__all__ = ["ClanHealthAnalyzer"]
