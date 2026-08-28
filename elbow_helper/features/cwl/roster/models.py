"""Data contracts for CWL roster planning."""

from __future__ import annotations

from dataclasses import dataclass
@dataclass(frozen=True)
class AssProfile:
    key: str
    label: str
    difficulty_weight: float
    missed_mode: str

    def missed_adjustment(self, missed_stars: float) -> float:
        if self.missed_mode == "high_linear":
            return 0.0 if missed_stars <= 1.0 else -2.0 * (missed_stars - 1.0)
        if self.missed_mode == "lower_linear":
            return 0.0 if missed_stars <= 2.0 else -missed_stars
        raise ValueError(f"Unsupported ASS missed-star mode: {self.missed_mode}")


ASS_PROFILE_HIGH = AssProfile(
    key="high_2026_06",
    label="High League Standard (Jun 2026)",
    difficulty_weight=0.4,
    missed_mode="high_linear",
)
ASS_PROFILE_LOWER = AssProfile(
    key="lower_2026_06",
    label="Lower League Standard (Jun 2026)",
    difficulty_weight=1.0,
    missed_mode="lower_linear",
)


def profile_for_league(league: str) -> AssProfile:
    normalized = " ".join(str(league or "").split()).casefold()
    if "champion" in normalized:
        return ASS_PROFILE_HIGH
    if "master" in normalized and normalized.split()[-1:] == ["i"]:
        return ASS_PROFILE_HIGH
    return ASS_PROFILE_LOWER


@dataclass
class AssSeasonMetric:
    season: str
    season_order: int
    latest_end_ts: int
    clan_code: str
    league: str
    profile: AssProfile
    player_tag: str
    player_name: str
    townhall: int
    wars: int = 0
    attacks_expected: int = 0
    attacks: int = 0
    stars: int = 0
    destruction_total: float = 0.0
    target_position_total: float = 0.0
    target_distance_total: float = 0.0
    defensive_position_total: float = 0.0
    difficulty_total: float = 0.0
    position_attacks: int = 0
    score: float | None = None
    projected_stars: float | None = None
    missed_stars: float | None = None
    missed_adjustment: float | None = None
    difficulty_adjustment: float | None = None
    average_destruction: float | None = None
    average_target_position: float | None = None
    average_target_distance: float | None = None
    average_defensive_position: float | None = None
    rank: int | None = None
    rank_total: int = 0

    @property
    def rank_label(self) -> str:
        if self.rank is None or self.rank_total <= 0:
            return "-"
        return f"{self.rank}/{self.rank_total}"
    @property
    def attacks_label(self) -> str:
        return f"{self.attacks}/{self.attacks_expected}"


@dataclass
class MegaAssMetric:
    clan_code: str
    profile: AssProfile
    player_tag: str
    player_name: str
    townhall: int
    seasons: tuple[AssSeasonMetric, ...]
    score: float
    total_attacks: int
    average_defensive_position: float | None
    rank: int | None = None
    rank_total: int = 0

    @property
    def latest(self) -> AssSeasonMetric:
        return max(self.seasons, key=lambda item: item.season_order)

    @property
    def rank_label(self) -> str:
        if self.rank is None or self.rank_total <= 0:
            return "-"
        return f"{self.rank}/{self.rank_total}"
