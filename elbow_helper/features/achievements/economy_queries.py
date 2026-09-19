"""Typed, read-only inventory, coin-history and raffle evidence."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .config import (
    DAILY_MIN_CHARS, DAILY_MSG_THRESHOLD, DAILY_REWARD, DAILY_REWARD_ELDER,
    MANUAL_CAP_CWL, MANUAL_CAP_ENCOURAGEMENT, MANUAL_CAP_TOTAL, SALARY_AMOUNT,
    TICKET_COST, TICKET_LIMIT_PER_MONTH,
)
from .definitions import COIN_REWARDS


MAX_COIN_TRANSACTIONS = 1_000
MAX_COIN_TRANSACTION_PAYLOAD_BYTES = 512 * 1024
MAX_RAFFLE_ROWS = 2_000


@dataclass(frozen=True, slots=True)
class MemberInventorySnapshot:
    observed_at: str
    member_id: int
    balance: int
    has_current_ticket: bool
    current_month_key: int


@dataclass(frozen=True, slots=True)
class CoinTransactionRow:
    transaction_id: int
    amount: int
    transaction_type: str
    reason: str | None
    actor_id: int | None
    created_at: int


@dataclass(frozen=True, slots=True)
class CoinTransactionSnapshot:
    observed_at: str
    member_id: int
    total_transactions: int
    rows: tuple[CoinTransactionRow, ...]
    complete: bool


@dataclass(frozen=True, slots=True)
class RaffleWinnerRow:
    winner_id: int
    member_id: int
    drawn_at: int
    draw_type: str
    reroll_of: int | None
    active: bool


@dataclass(frozen=True, slots=True)
class RaffleSnapshot:
    observed_at: str
    month_key: int
    month_label: str
    prize: str | None
    configured_winners: int
    total_tickets: int
    ticket_member_ids: tuple[int, ...]
    total_winner_rows: int
    winners: tuple[RaffleWinnerRow, ...]
    complete: bool


@dataclass(frozen=True, slots=True)
class EconomyRulesSnapshot:
    observed_at: str
    daily_message_threshold: int
    daily_minimum_characters: int
    daily_member_reward: int
    daily_elder_reward: int
    elder_monthly_salary: int
    ticket_cost: int
    ticket_limit_per_month: int
    manual_cwl_monthly_cap: int
    manual_encouragement_monthly_cap: int
    manual_combined_monthly_cap: int
    achievement_rewards: tuple[tuple[str, int], ...]


class AchievementEconomyQueries:
    """Read economy state without exposing mutation internals."""

    def __init__(
        self, database_path: Path,
        *, clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = Path(database_path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def rules(self) -> EconomyRulesSnapshot:
        """Return configured economy rules without reading mutable member state."""
        return EconomyRulesSnapshot(
            observed_at=self._observed().isoformat(),
            daily_message_threshold=DAILY_MSG_THRESHOLD,
            daily_minimum_characters=DAILY_MIN_CHARS,
            daily_member_reward=DAILY_REWARD,
            daily_elder_reward=DAILY_REWARD_ELDER,
            elder_monthly_salary=SALARY_AMOUNT,
            ticket_cost=TICKET_COST,
            ticket_limit_per_month=TICKET_LIMIT_PER_MONTH,
            manual_cwl_monthly_cap=MANUAL_CAP_CWL,
            manual_encouragement_monthly_cap=MANUAL_CAP_ENCOURAGEMENT,
            manual_combined_monthly_cap=MANUAL_CAP_TOTAL,
            achievement_rewards=tuple(sorted(COIN_REWARDS.items())),
        )

    def member_inventory(self, member_id: int) -> MemberInventorySnapshot:
        if type(member_id) is not int or member_id <= 0:
            raise ValueError("Invalid inventory member")
        observed = self._observed()
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT balance, last_ticket_month FROM user_coins "
                    "WHERE user_id = ?",
                    (member_id,),
                ).fetchone()
        except sqlite3.Error as error:
            raise RuntimeError("Inventory data could not be read") from error
        month_key = observed.year * 12 + observed.month
        return MemberInventorySnapshot(
            observed.isoformat(), member_id,
            int(row[0]) if row is not None else 0,
            bool(row is not None and row[1] == month_key), month_key,
        )

    def coin_transactions(self, member_id: int) -> CoinTransactionSnapshot:
        if type(member_id) is not int or member_id <= 0:
            raise ValueError("Invalid coin-history member")
        observed = self._observed()
        try:
            with closing(self._connect()) as connection:
                total = int(connection.execute(
                    "SELECT COUNT(*) FROM coin_transactions WHERE user_id = ?",
                    (member_id,),
                ).fetchone()[0])
                rows = connection.execute(
                    """
                    SELECT id, amount, type, reason, actor_id, created_at
                    FROM coin_transactions
                    WHERE user_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (member_id, MAX_COIN_TRANSACTIONS),
                ).fetchall()
        except sqlite3.Error as error:
            raise RuntimeError("Coin history could not be read") from error
        parsed_rows = []
        retained_bytes = 0
        for row in rows:
            parsed = CoinTransactionRow(
                transaction_id=int(row[0]), amount=int(row[1]),
                transaction_type=str(row[2]), reason=row[3],
                actor_id=int(row[4]) if row[4] is not None else None,
                created_at=int(row[5]),
            )
            row_bytes = len(json.dumps(
                {
                    "transaction_id": parsed.transaction_id,
                    "amount": parsed.amount,
                    "transaction_type": parsed.transaction_type,
                    "reason": parsed.reason,
                    "actor_id": parsed.actor_id,
                    "created_at": parsed.created_at,
                }, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8"))
            if retained_bytes + row_bytes > MAX_COIN_TRANSACTION_PAYLOAD_BYTES:
                break
            retained_bytes += row_bytes
            parsed_rows.append(parsed)
        return CoinTransactionSnapshot(
            observed.isoformat(), member_id, total, tuple(parsed_rows),
            total == len(parsed_rows),
        )

    def raffle(self, month: str | None = None) -> RaffleSnapshot:
        observed = self._observed()
        month_key, month_label = _raffle_month(month, observed)
        try:
            with closing(self._connect()) as connection:
                meta = dict(connection.execute(
                    "SELECT key, value FROM economy_meta WHERE key IN (?, ?)",
                    (f"reward_{month_key}", f"winners_{month_key}"),
                ).fetchall())
                total_tickets = int(connection.execute(
                    "SELECT COUNT(*) FROM raffle_tickets WHERE month_key = ?",
                    (month_key,),
                ).fetchone()[0])
                tickets = tuple(int(row[0]) for row in connection.execute(
                    "SELECT user_id FROM raffle_tickets WHERE month_key = ? "
                    "ORDER BY user_id ASC LIMIT ?",
                    (month_key, MAX_RAFFLE_ROWS),
                ).fetchall())
                total_winners = int(connection.execute(
                    "SELECT COUNT(*) FROM raffle_winners WHERE month_key = ?",
                    (month_key,),
                ).fetchone()[0])
                winners = tuple(RaffleWinnerRow(
                    winner_id=int(row[0]), member_id=int(row[1]),
                    drawn_at=int(row[2]), draw_type=str(row[3]),
                    reroll_of=int(row[4]) if row[4] is not None else None,
                    active=bool(row[5]),
                ) for row in connection.execute(
                    """
                    SELECT id, user_id, drawn_at, draw_type, reroll_of, is_active
                    FROM raffle_winners
                    WHERE month_key = ?
                    ORDER BY drawn_at ASC, id ASC
                    LIMIT ?
                    """,
                    (month_key, MAX_RAFFLE_ROWS),
                ).fetchall())
        except sqlite3.Error as error:
            raise RuntimeError("Raffle data could not be read") from error
        raw_winners = meta.get(f"winners_{month_key}")
        try:
            configured_winners = max(1, int(raw_winners)) if raw_winners else 1
        except (TypeError, ValueError):
            configured_winners = 1
        raw_prize = meta.get(f"reward_{month_key}")
        return RaffleSnapshot(
            observed.isoformat(), month_key, month_label,
            str(raw_prize) if raw_prize else None, configured_winners,
            total_tickets, tickets, total_winners, winners,
            total_tickets == len(tickets) and total_winners == len(winners),
        )

    def _observed(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _connect(self) -> sqlite3.Connection:
        if not self._path.is_file():
            raise RuntimeError("Achievement data is unavailable")
        return sqlite3.connect(
            f"{self._path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0,
        )


def _raffle_month(value: str | None, observed: datetime) -> tuple[int, str]:
    if value is None:
        selected = observed
    else:
        if not isinstance(value, str) or len(value) != 7 or value[4] != "-":
            raise ValueError("Invalid raffle month")
        try:
            selected = datetime(
                int(value[:4]), int(value[5:]), 1, tzinfo=timezone.utc,
            )
        except ValueError as error:
            raise ValueError("Invalid raffle month") from error
    return selected.year * 12 + selected.month, selected.strftime("%B %Y")


__all__ = [
    "AchievementEconomyQueries", "CoinTransactionRow",
    "CoinTransactionSnapshot", "EconomyRulesSnapshot", "MAX_COIN_TRANSACTIONS",
    "MAX_COIN_TRANSACTION_PAYLOAD_BYTES", "MAX_RAFFLE_ROWS",
    "MemberInventorySnapshot", "RaffleSnapshot", "RaffleWinnerRow",
]
