"""Clan Health scoring and workbook analysis service."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..config import CLAN_PROFILE_BY_CODE
from ..config import PROFILE_NAMES
from ..database import ClanHealthRepository
from .flags import ClanHealthFlagMixin
from .overlays import ClanHealthOverlayMixin
from .sheets import ClanHealthSheetMixin
from .verdicts import assess_war_participation
from .verdicts import normalize_player_verdict


class ClanHealthAnalyzer(
    ClanHealthOverlayMixin,
    ClanHealthFlagMixin,
    ClanHealthSheetMixin,
):
    """Apply scoring policy to data supplied by Clan Health storage."""

    def __init__(self, repository: ClanHealthRepository):
        self.repository = repository

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

    def player_trend_history(
        self,
        *,
        player_tag: str,
        up_to_season_key: str,
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        rows = self.repository.player_trend_rows(
            player_tag=player_tag,
            up_to_season_key=up_to_season_key,
            limit=limit,
        )
        for row in rows:
            row["status"] = normalize_player_verdict(row.get("status"))
            row["consistency_score"] = (
                self._consistency_score_from_row(row)
            )
        return rows

    apply_progression_fallback = (
        ClanHealthOverlayMixin._apply_progression_delta_fallback
    )
    apply_war_activity = ClanHealthOverlayMixin._apply_family_war_activity
    apply_raid_activity = ClanHealthOverlayMixin._apply_family_raid_activity
    apply_donation_activity = (
        ClanHealthOverlayMixin._apply_family_donation_activity
    )
    @staticmethod
    def report_row_is_sparse(row: dict[str, Any] | None) -> bool:
        return ClanHealthOverlayMixin._report_row_is_sparse(row)

    apply_flags = ClanHealthFlagMixin._apply_flags
    build_sheets = ClanHealthSheetMixin._build_sheets


__all__ = ["ClanHealthAnalyzer"]
