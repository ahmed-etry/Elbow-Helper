"""Typed, read-only achievement evidence owned by the feature."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any

from elbow_helper.configuration.roles import LEAD

from .definitions import ALL_ACHIEVEMENTS
from .economy_queries import (
    AchievementEconomyQueries, CoinTransactionRow, CoinTransactionSnapshot,
    EconomyRulesSnapshot, MAX_COIN_TRANSACTIONS,
    MAX_COIN_TRANSACTION_PAYLOAD_BYTES, MAX_RAFFLE_ROWS, MemberInventorySnapshot,
    RaffleSnapshot, RaffleWinnerRow,
)


STAT_FIELDS = (
    "message_count", "emoji_count", "reaction_count", "voice_hours",
    "silent_voice_seconds", "role_pings", "meme_posts",
    "clan_transfer_count", "active_channels", "activity_streak",
    "weekly_activity_count", "monthly_activity_count", "early_bird_count",
    "night_owl_count",
)
_COUNTER_FIELDS = {
    "chatterbox": "message_count",
    "keyboard_warrior": "message_count",
    "emoji_enthusiast": "emoji_count",
    "react_lord": "reaction_count",
    "ping_collector": "role_pings",
    "meme_dealer": "meme_posts",
    "daily_streaker": "activity_streak",
    "weekly_warrior": "weekly_activity_count",
    "monthly_master": "monthly_activity_count",
    "early_bird": "early_bird_count",
    "night_owl": "night_owl_count",
    "marathoner": "voice_hours",
    "clan_hopper": "clan_transfer_count",
}
_CHANNEL_COUNTERS = frozenset({"social_butterfly", "channel_explorer"})
_MEMBERSHIP_DAYS = frozenset({"one_of_us", "veteran"})


@dataclass(frozen=True, slots=True)
class AchievementProgressRow:
    achievement_id: str
    name: str
    description: str
    required_count: int
    progress_kind: str
    current_count: int | float
    completed: bool
    completed_at: int | None


@dataclass(frozen=True, slots=True)
class MemberAchievementSnapshot:
    observed_at: str
    member_id: int
    completed_count: int
    total_count: int
    rows: tuple[AchievementProgressRow, ...]


@dataclass(frozen=True, slots=True)
class AchievementCountRow:
    member_id: int
    achievement_count: int


@dataclass(frozen=True, slots=True)
class AchievementCountSnapshot:
    observed_at: str
    total_achievements: int
    rows: tuple[AchievementCountRow, ...]


def achievement_progress_value(
    achievement_id: str,
    stats: tuple[Any, ...],
    *,
    joined_at: datetime | None,
    observed_at: datetime,
) -> tuple[int | float, str]:
    """Return the existing tracked value and whether progress is measurable."""
    values = dict(zip(STAT_FIELDS, stats, strict=True))
    if achievement_id in _COUNTER_FIELDS:
        value = values[_COUNTER_FIELDS[achievement_id]] or 0
        if achievement_id == "marathoner":
            value = int(float(value))
        return value, "counter"
    if achievement_id == "silent_lurker":
        return (
            round(float(values["silent_voice_seconds"] or 0) / 3600.0, 2),
            "counter",
        )
    if achievement_id in _CHANNEL_COUNTERS:
        return len(str(values["active_channels"] or "").split()), "counter"
    if achievement_id in _MEMBERSHIP_DAYS:
        if joined_at is None:
            return 0, "membership_days"
        joined = joined_at
        if joined.tzinfo is None:
            joined = joined.replace(tzinfo=timezone.utc)
        return (
            max(0, (observed_at - joined.astimezone(timezone.utc)).days),
            "membership_days",
        )
    return 0, "completion_only"


def achievement_leaderboard_eligible(
    role_ids: set[int] | frozenset[int], *, include_leadership: bool = False,
) -> bool:
    """Apply the existing member-leaderboard leadership exclusion."""
    return include_leadership or not bool(role_ids & LEAD)


class AchievementQueries:
    """Read progress and counts without exposing economy or mutation internals."""

    def __init__(
        self, database_path: Path,
        *, clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = Path(database_path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.economy = AchievementEconomyQueries(
            self._path, clock=self._clock,
        )

    def member_progress(
        self, member_id: int, *, joined_at: datetime | None,
    ) -> MemberAchievementSnapshot:
        if type(member_id) is not int or member_id <= 0:
            raise ValueError("Invalid achievement member")
        observed = self._observed()
        try:
            with closing(self._connect()) as connection:
                completed = {
                    str(row[0]): int(row[1])
                    for row in connection.execute(
                        """
                        SELECT achievement_id, completed_date
                        FROM user_achievements
                        WHERE user_id = ? AND completed_date IS NOT NULL
                        """,
                        (member_id,),
                    ).fetchall()
                }
                stats_row = connection.execute(
                    f"SELECT {', '.join(STAT_FIELDS)} "
                    "FROM user_stats WHERE user_id = ?",
                    (member_id,),
                ).fetchone()
        except sqlite3.Error as error:
            raise RuntimeError("Achievement data could not be read") from error
        stats = tuple(stats_row) if stats_row is not None else (
            0, 0, 0, 0.0, 0, 0, 0, 0, "", 0, 0, 0, 0, 0,
        )
        rows = []
        for raw_id, raw_name, raw_description, raw_required, *_ in sorted(
            ALL_ACHIEVEMENTS, key=lambda item: (str(item[1]), str(item[0])),
        ):
            achievement_id = str(raw_id)
            required = max(1, int(raw_required or 1))
            current, progress_kind = achievement_progress_value(
                achievement_id, stats, joined_at=joined_at,
                observed_at=observed,
            )
            completed_at = completed.get(achievement_id)
            rows.append(AchievementProgressRow(
                achievement_id, str(raw_name), str(raw_description), required,
                progress_kind, current, completed_at is not None, completed_at,
            ))
        return MemberAchievementSnapshot(
            observed.isoformat(), member_id,
            sum(row.completed for row in rows), len(rows), tuple(rows),
        )

    def leaderboard_counts(self) -> AchievementCountSnapshot:
        observed = self._observed()
        known_ids = tuple(str(row[0]) for row in ALL_ACHIEVEMENTS)
        placeholders = ",".join("?" for _ in known_ids)
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    f"""
                    SELECT ua.user_id, COUNT(*) AS achievement_count
                    FROM user_achievements AS ua
                    INNER JOIN achievements AS a ON a.id = ua.achievement_id
                    WHERE ua.completed_date IS NOT NULL
                      AND ua.achievement_id IN ({placeholders})
                    GROUP BY ua.user_id
                    ORDER BY achievement_count DESC, ua.user_id ASC
                    """,
                    known_ids,
                ).fetchall()
        except sqlite3.Error as error:
            raise RuntimeError("Achievement data could not be read") from error
        return AchievementCountSnapshot(
            observed.isoformat(), len(ALL_ACHIEVEMENTS),
            tuple(AchievementCountRow(int(row[0]), int(row[1])) for row in rows),
        )

    def member_inventory(self, member_id: int) -> MemberInventorySnapshot:
        return self.economy.member_inventory(member_id)

    def coin_transactions(self, member_id: int) -> CoinTransactionSnapshot:
        return self.economy.coin_transactions(member_id)

    def raffle(self, month: str | None = None) -> RaffleSnapshot:
        return self.economy.raffle(month)

    def economy_rules(self) -> EconomyRulesSnapshot:
        return self.economy.rules()

    def _observed(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _connect(self) -> sqlite3.Connection:
        if not self._path.is_file():
            raise RuntimeError("Achievement data is unavailable")
        return sqlite3.connect(
            f"{self._path.resolve().as_uri()}?mode=ro",
            uri=True, timeout=5.0,
        )


__all__ = [
    "AchievementCountRow", "AchievementCountSnapshot", "CoinTransactionRow",
    "CoinTransactionSnapshot", "EconomyRulesSnapshot", "MAX_COIN_TRANSACTIONS",
    "MAX_COIN_TRANSACTION_PAYLOAD_BYTES",
    "MAX_RAFFLE_ROWS",
    "AchievementProgressRow", "AchievementQueries",
    "MemberAchievementSnapshot", "MemberInventorySnapshot", "STAT_FIELDS",
    "RaffleSnapshot", "RaffleWinnerRow",
    "achievement_leaderboard_eligible", "achievement_progress_value",
]
