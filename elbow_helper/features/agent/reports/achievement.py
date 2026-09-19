"""Restart-persistent achievement evidence reports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import math
import re
from typing import Any

from elbow_helper.features.achievements.queries import MemberAchievementSnapshot
from elbow_helper.features.achievements.economy_queries import (
    CoinTransactionSnapshot, RaffleSnapshot,
)


@dataclass(frozen=True, slots=True)
class AchievementProgressReport:
    report_id: str
    guild_id: int
    member_name: str
    snapshot: MemberAchievementSnapshot
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        value = self.snapshot
        if (
            not _valid_identity(self.report_id, self.guild_id)
            or not isinstance(self.member_name, str) or not self.member_name
            or len(self.member_name) > 100
            or not isinstance(value.observed_at, str) or not value.observed_at
            or type(value.member_id) is not int or value.member_id <= 0
            or type(value.completed_count) is not int
            or value.completed_count != sum(row.completed for row in value.rows)
            or type(value.total_count) is not int
            or value.total_count != len(value.rows)
            or value.total_count > 100
            or len({row.achievement_id for row in value.rows}) != len(value.rows)
            or any(not _valid_progress_row(row) for row in value.rows)
        ):
            raise ValueError("Invalid achievement progress report")
        object.__setattr__(
            self, "retained_bytes", _payload_bytes(self.storage_payload()),
        )

    def storage_payload(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id, "guild_id": self.guild_id,
            "member_name": self.member_name,
            "snapshot": asdict(self.snapshot),
        }

    def manifest(self) -> dict[str, Any]:
        value = self.snapshot
        return {
            "report_id": self.report_id, "kind": "achievement_progress",
            "observed_at": value.observed_at,
            "member_id": value.member_id, "member_name": self.member_name,
            "completed_count": value.completed_count,
            "in_progress_count": value.total_count - value.completed_count,
            "total_count": value.total_count,
        }

    def page(
        self, *, status: str = "all", offset: int = 0, limit: int = 25,
    ) -> dict[str, Any]:
        if (
            status not in {"all", "completed", "in_progress"}
            or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 25
        ):
            raise ValueError("Invalid achievement progress page")
        rows = tuple(row for row in self.snapshot.rows if (
            status == "all"
            or status == "completed" and row.completed
            or status == "in_progress" and not row.completed
        ))
        return {
            **self.manifest(), "status_filter": status,
            "matching_rows": len(rows),
            "achievements": [
                asdict(row) for row in rows[offset:offset + limit]
            ],
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "complete_snapshot": True,
        }


@dataclass(frozen=True, slots=True)
class AchievementLeaderboardMember:
    member_id: int
    member_name: str
    achievement_count: int


@dataclass(frozen=True, slots=True)
class AchievementLeaderboardReport:
    report_id: str
    guild_id: int
    observed_at: str
    total_achievements: int
    rows: tuple[AchievementLeaderboardMember, ...]
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        expected_order = tuple(sorted(
            self.rows,
            key=lambda row: (-row.achievement_count, row.member_id),
        ))
        if (
            not _valid_identity(self.report_id, self.guild_id)
            or not isinstance(self.observed_at, str) or not self.observed_at
            or type(self.total_achievements) is not int
            or not 0 <= self.total_achievements <= 100
            or len(self.rows) > 10_000 or self.rows != expected_order
            or len({row.member_id for row in self.rows}) != len(self.rows)
            or any(
                type(row.member_id) is not int or row.member_id <= 0
                or not isinstance(row.member_name, str) or not row.member_name
                or len(row.member_name) > 100
                or type(row.achievement_count) is not int
                or not 1 <= row.achievement_count <= self.total_achievements
                for row in self.rows
            )
        ):
            raise ValueError("Invalid achievement leaderboard report")
        object.__setattr__(
            self, "retained_bytes", _payload_bytes(self.storage_payload()),
        )

    def storage_payload(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id, "guild_id": self.guild_id,
            "observed_at": self.observed_at,
            "total_achievements": self.total_achievements,
            "rows": [asdict(row) for row in self.rows],
        }

    def manifest(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "kind": "achievement_leaderboard",
            "observed_at": self.observed_at,
            "total_achievements": self.total_achievements,
            "represented_members": len(self.rows),
            "leadership_included": False,
        }

    def page(self, *, offset: int = 0, limit: int = 25) -> dict[str, Any]:
        if (
            type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 25
        ):
            raise ValueError("Invalid achievement leaderboard page")
        return {
            **self.manifest(), "matching_rows": len(self.rows),
            "members": [
                {"rank": index + 1, **asdict(row)}
                for index, row in enumerate(
                    self.rows[offset:offset + limit], start=offset,
                )
            ],
            "next_offset": (
                offset + limit if offset + limit < len(self.rows) else None
            ),
            "complete_snapshot": True,
        }


@dataclass(frozen=True, slots=True)
class CoinTransactionReport:
    report_id: str
    guild_id: int
    member_name: str
    snapshot: CoinTransactionSnapshot
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        value = self.snapshot
        expected_order = tuple(sorted(
            value.rows, key=lambda row: row.transaction_id, reverse=True,
        ))
        if (
            not _valid_identity(self.report_id, self.guild_id)
            or not isinstance(self.member_name, str) or not self.member_name
            or len(self.member_name) > 100
            or not isinstance(value.observed_at, str) or not value.observed_at
            or type(value.member_id) is not int or value.member_id <= 0
            or type(value.total_transactions) is not int
            or value.total_transactions < len(value.rows)
            or len(value.rows) > 1_000
            or value.rows != expected_order
            or len({row.transaction_id for row in value.rows}) != len(value.rows)
            or type(value.complete) is not bool
            or value.complete != (value.total_transactions == len(value.rows))
            or any(not _valid_coin_transaction(row) for row in value.rows)
        ):
            raise ValueError("Invalid coin transaction report")
        object.__setattr__(
            self, "retained_bytes", _payload_bytes(self.storage_payload()),
        )

    def storage_payload(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id, "guild_id": self.guild_id,
            "member_name": self.member_name,
            "snapshot": asdict(self.snapshot),
        }

    def manifest(self) -> dict[str, Any]:
        value = self.snapshot
        return {
            "report_id": self.report_id, "kind": "coin_transactions",
            "observed_at": value.observed_at,
            "member_id": value.member_id, "member_name": self.member_name,
            "total_transactions": value.total_transactions,
            "retained_transactions": len(value.rows),
            "complete_snapshot": value.complete,
            "order": "newest_first",
        }

    def page(self, *, offset: int = 0, limit: int = 25) -> dict[str, Any]:
        if (
            type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 25
        ):
            raise ValueError("Invalid coin transaction page")
        rows = self.snapshot.rows
        return {
            **self.manifest(),
            "transactions": [asdict(row) for row in rows[offset:offset + limit]],
            "next_offset": offset + limit if offset + limit < len(rows) else None,
        }


@dataclass(frozen=True, slots=True)
class RaffleReport:
    report_id: str
    guild_id: int
    snapshot: RaffleSnapshot
    member_names: tuple[tuple[int, str], ...]
    retained_bytes: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        value = self.snapshot
        expected_winners = tuple(sorted(
            value.winners, key=lambda row: (row.drawn_at, row.winner_id),
        ))
        known_ids = set(value.ticket_member_ids) | {
            row.member_id for row in value.winners
        }
        if (
            not _valid_identity(self.report_id, self.guild_id)
            or not isinstance(value.observed_at, str) or not value.observed_at
            or type(value.month_key) is not int or value.month_key <= 0
            or not isinstance(value.month_label, str) or not value.month_label
            or len(value.month_label) > 30
            or (
                value.prize is not None
                and (not isinstance(value.prize, str) or len(value.prize) > 2_000)
            )
            or type(value.configured_winners) is not int
            or value.configured_winners < 1
            or type(value.total_tickets) is not int
            or value.total_tickets < len(value.ticket_member_ids)
            or len(value.ticket_member_ids) > 2_000
            or tuple(sorted(set(value.ticket_member_ids)))
            != value.ticket_member_ids
            or any(type(member_id) is not int or member_id <= 0
                   for member_id in value.ticket_member_ids)
            or type(value.total_winner_rows) is not int
            or value.total_winner_rows < len(value.winners)
            or len(value.winners) > 2_000
            or value.winners != expected_winners
            or len({row.winner_id for row in value.winners}) != len(value.winners)
            or any(not _valid_raffle_winner(row) for row in value.winners)
            or type(value.complete) is not bool
            or value.complete != (
                value.total_tickets == len(value.ticket_member_ids)
                and value.total_winner_rows == len(value.winners)
            )
            or tuple(sorted(self.member_names)) != self.member_names
            or len({item[0] for item in self.member_names}) != len(self.member_names)
            or any(
                type(member_id) is not int or member_id not in known_ids
                or not isinstance(name, str) or not name or len(name) > 100
                for member_id, name in self.member_names
            )
        ):
            raise ValueError("Invalid raffle report")
        object.__setattr__(
            self, "retained_bytes", _payload_bytes(self.storage_payload()),
        )

    def storage_payload(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id, "guild_id": self.guild_id,
            "snapshot": asdict(self.snapshot),
            "member_names": [list(item) for item in self.member_names],
        }

    def manifest(self) -> dict[str, Any]:
        value = self.snapshot
        known = dict(self.member_names)
        represented_ids = set(value.ticket_member_ids) | {
            row.member_id for row in value.winners
        }
        return {
            "report_id": self.report_id, "kind": "raffle",
            "observed_at": value.observed_at,
            "month_key": value.month_key, "month_label": value.month_label,
            "prize": value.prize,
            "configured_winners": value.configured_winners,
            "total_tickets": value.total_tickets,
            "total_winner_rows": value.total_winner_rows,
            "unresolved_member_count": len(represented_ids - set(known)),
            "complete_snapshot": value.complete,
        }

    def page(
        self, *, ticket_offset: int = 0, winner_offset: int = 0,
        limit: int = 25,
    ) -> dict[str, Any]:
        if (
            type(ticket_offset) is not int or ticket_offset < 0
            or type(winner_offset) is not int or winner_offset < 0
            or type(limit) is not int or not 1 <= limit <= 25
        ):
            raise ValueError("Invalid raffle page")
        value = self.snapshot
        names = dict(self.member_names)
        tickets = value.ticket_member_ids
        winners = value.winners
        return {
            **self.manifest(),
            "tickets": [
                {"member_id": member_id, "member_name": names.get(member_id)}
                for member_id in tickets[ticket_offset:ticket_offset + limit]
            ],
            "winners": [
                {**asdict(row), "member_name": names.get(row.member_id)}
                for row in winners[winner_offset:winner_offset + limit]
            ],
            "next_ticket_offset": (
                ticket_offset + limit
                if ticket_offset + limit < len(tickets) else None
            ),
            "next_winner_offset": (
                winner_offset + limit
                if winner_offset + limit < len(winners) else None
            ),
        }


def _valid_identity(report_id: Any, guild_id: Any) -> bool:
    return bool(
        isinstance(report_id, str) and report_id and len(report_id) <= 32
        and type(guild_id) is int and guild_id > 0
    )


def _valid_progress_row(row: Any) -> bool:
    return bool(
        isinstance(row.achievement_id, str)
        and re.fullmatch(r"[a-z0-9_]{1,64}", row.achievement_id)
        and isinstance(row.name, str) and row.name and len(row.name) <= 100
        and isinstance(row.description, str) and len(row.description) <= 500
        and type(row.required_count) is int and row.required_count > 0
        and row.progress_kind in {
            "counter", "membership_days", "completion_only",
        }
        and isinstance(row.current_count, (int, float))
        and not isinstance(row.current_count, bool)
        and math.isfinite(float(row.current_count))
        and row.current_count >= 0
        and type(row.completed) is bool
        and (
            row.completed_at is None
            or type(row.completed_at) is int and row.completed_at >= 0
        )
        and row.completed == (row.completed_at is not None)
    )


def _valid_coin_transaction(row: Any) -> bool:
    return bool(
        type(row.transaction_id) is int and row.transaction_id > 0
        and type(row.amount) is int
        and isinstance(row.transaction_type, str) and row.transaction_type
        and len(row.transaction_type) <= 100
        and (
            row.reason is None
            or isinstance(row.reason, str) and len(row.reason) <= 2_000
        )
        and (
            row.actor_id is None
            or type(row.actor_id) is int and row.actor_id > 0
        )
        and type(row.created_at) is int and row.created_at >= 0
    )


def _valid_raffle_winner(row: Any) -> bool:
    return bool(
        type(row.winner_id) is int and row.winner_id > 0
        and type(row.member_id) is int and row.member_id > 0
        and type(row.drawn_at) is int and row.drawn_at >= 0
        and isinstance(row.draw_type, str) and row.draw_type
        and len(row.draw_type) <= 50
        and (
            row.reroll_of is None
            or type(row.reroll_of) is int and row.reroll_of > 0
        )
        and type(row.active) is bool
    )


def _payload_bytes(value: dict[str, Any]) -> int:
    return len(json.dumps(
        value, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8"))


__all__ = [
    "AchievementLeaderboardMember", "AchievementLeaderboardReport",
    "AchievementProgressReport", "CoinTransactionReport", "RaffleReport",
]
