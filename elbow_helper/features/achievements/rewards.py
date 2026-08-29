"""Supported reward operations for other bot features."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any

import discord


@dataclass(frozen=True, slots=True)
class RewardBatchResult:
    reward_kind: str
    granted_ids: tuple[int, ...]
    already_granted_ids: tuple[int, ...]
    skipped: tuple[tuple[int, str], ...]
    elder_grants: tuple[tuple[int, int], ...]
    member_grants: tuple[tuple[int, int], ...]


class AchievementRewardService:
    """Apply transactional economy rewards through a stable public contract."""

    def __init__(self, owner: Any):
        self._owner = owner

    def cwl_exclusion_reason(
        self,
        member: discord.Member,
    ) -> str | None:
        if self._owner._is_leadership_any(member):
            return "leadership excluded"
        return None

    async def award_achievement(
        self,
        user_id: int,
        achievement_id: str,
        *,
        announce: bool = True,
    ) -> None:
        await self._owner.award_achievement(
            user_id,
            achievement_id,
            announce=announce,
        )

    async def track_hibernation_survivor(
        self,
        user_id: int,
    ) -> None:
        await self._owner.track_hibernation_survivor(user_id)

    async def grant_cwl_rewards(
        self,
        members: list[discord.Member],
        *,
        reward_kind: str,
        reason: str,
        actor_id: int,
    ) -> RewardBatchResult:
        if reward_kind not in {"ticket", "coins"}:
            raise ValueError(f"Unsupported reward kind: {reward_kind}")

        async def apply(cursor) -> RewardBatchResult:
            granted_ids: list[int] = []
            already_granted_ids: list[int] = []
            skipped: list[tuple[int, str]] = []
            elder_grants: list[tuple[int, int]] = []
            member_grants: list[tuple[int, int]] = []
            for member in members:
                exclusion_reason = self.cwl_exclusion_reason(member)
                if exclusion_reason:
                    skipped.append((member.id, exclusion_reason))
                    continue
                if reward_kind == "ticket":
                    granted, message = await self._grant_ticket(
                        cursor,
                        member.id,
                        reason,
                    )
                    if not granted:
                        skipped.append((member.id, message))
                        continue
                    granted_ids.append(member.id)
                    continue

                amount = 10 if self._owner._is_elder(member) else 5
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO cwl_reward_grants
                        (reason, user_id, reward_kind, created_at)
                    VALUES (?, ?, 'coins', ?)
                    """,
                    (
                        reason,
                        member.id,
                        int(datetime.now(timezone.utc).timestamp()),
                    ),
                )
                if cursor.rowcount == 0:
                    already_granted_ids.append(member.id)
                    skipped.append((member.id, "reward already granted"))
                    continue
                await self._owner._add_coins(
                    cursor,
                    member.id,
                    amount,
                    "bonus_cwl",
                    reason,
                    actor_id,
                )
                granted_ids.append(member.id)
                target = (
                    elder_grants
                    if self._owner._is_elder(member)
                    else member_grants
                )
                target.append((member.id, amount))

            return RewardBatchResult(
                reward_kind=reward_kind,
                granted_ids=tuple(granted_ids),
                already_granted_ids=tuple(already_granted_ids),
                skipped=tuple(skipped),
                elder_grants=tuple(elder_grants),
                member_grants=tuple(member_grants),
            )

        return await self._owner._retry_db_operation(apply)

    async def _grant_ticket(
        self,
        cursor,
        user_id: int,
        reason: str,
    ) -> tuple[bool, str]:
        await self._owner._ensure_coin_row(cursor, user_id)
        month_key = self._owner._month_key()
        cursor.execute(
            "SELECT last_ticket_month FROM user_coins WHERE user_id = ?",
            (user_id,),
        )
        row = cursor.fetchone()
        last_ticket_month = row[0] if row else None
        if last_ticket_month == month_key:
            return False, "User already has a ticket this month."
        cursor.execute(
            "UPDATE user_coins SET last_ticket_month = ? WHERE user_id = ?",
            (month_key, user_id),
        )
        cursor.execute(
            "INSERT OR REPLACE INTO raffle_tickets (month_key, user_id) "
            "VALUES (?, ?)",
            (month_key, user_id),
        )
        cursor.execute(
            """
            INSERT INTO coin_transactions
                (user_id, amount, type, reason, actor_id, created_at)
            VALUES (?, 0, ?, ?, ?, ?)
            """,
            (
                user_id,
                "ticket_grant",
                reason,
                None,
                int(datetime.now(timezone.utc).timestamp()),
            ),
        )
        return True, "Ticket granted for this month."
