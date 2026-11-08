from __future__ import annotations

from pathlib import Path
import sqlite3
from tests.temp_directory import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

from elbow_helper.features.achievements.rewards import AchievementRewardService
from elbow_helper.features.cwl.bonus.service import BonusReportService



class BonusReportServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_workflow_builds_retains_and_publishes_once(self) -> None:
        analysis = MagicMock()
        analysis.seasons.return_value = ["2026-07"]
        analysis.analyze_clan.return_value = (
            [{
                "clan": "BEH",
                "rank": 1,
                "player_name": "Ahmad",
                "attack_count": 1,
                "avg_adjusted_delta": 1.0,
                "total_adjusted_delta": 1.0,
                "total_actual": 3.0,
                "total_expected": 2.0,
                "total_base_delta": 1.0,
                "total_adjustment": 0.0,
            }],
            [],
            [{
                "clan": "BEH",
                "round": 1,
                "player_name": "Ahmad",
                "war_tag": "#WAR",
                "attacker_th": 18,
                "defender_tag": "#D",
                "defender_th": 18,
                "stars": 3,
                "destruction": 100.0,
                "actual_score": 3.0,
                "expected_score": 2.0,
                "th_gap": 0,
                "base_delta": 1.0,
                "delta_adjustment": 0.0,
                "adjusted_delta": 1.0,
                "star_gain": 3,
                "flags": "",
            }],
            [],
            [],
        )
        config = MagicMock()
        config.load.return_value = ({"clans": {}}, [])
        writer = MagicMock()
        writer.guide_sheet.return_value = [["Guide"]]
        publisher = MagicMock()
        publisher.upload_workbook = AsyncMock(
            return_value=("https://docs.google.com/spreadsheets/d/id/edit", None)
        )
        exports = MagicMock()
        exports.cleanup.return_value = (0, None)
        with TemporaryDirectory() as temp_dir:
            exports.temporary_path.side_effect = (
                lambda prefix: Path(temp_dir) / f"{prefix}.xlsx"
            )
            service = BonusReportService(
                analysis,
                config,
                writer,
                publisher,
                exports,
            )
            report = await service.create("BEH", "2026-07")

        self.assertEqual(report.eligible_count, 1)
        self.assertEqual(report.attack_count, 1)
        self.assertEqual(report.selected_clans, ("BEH",))
        written_sheets = writer.write.call_args.args[1]
        self.assertEqual(
            [name for name, _ in written_sheets],
            ["Guide", "Summary", "Raw Attacks"],
        )
        raw_headers = written_sheets[2][1][0]
        self.assertIn(
            "TH Difference (Defender − Attacker)",
            raw_headers,
        )
        exports.cleanup.assert_called_once_with(
            "*.xlsx"
        )
        publisher.upload_workbook.assert_awaited_once()


class _RewardOwner:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript(
            """
            CREATE TABLE user_coins (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 0,
                daily_msg_count INTEGER NOT NULL DEFAULT 0,
                last_ticket_month INTEGER
            );
            CREATE TABLE raffle_tickets (
                month_key INTEGER,
                user_id INTEGER,
                PRIMARY KEY (month_key, user_id)
            );
            CREATE TABLE coin_transactions (
                user_id INTEGER,
                amount INTEGER,
                type TEXT,
                reason TEXT,
                actor_id INTEGER,
                created_at INTEGER
            );
            CREATE TABLE cwl_reward_grants (
                reason TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                reward_kind TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (reason, user_id, reward_kind)
            );
            """
        )

    async def _retry_db_operation(self, callback):
        cursor = self.connection.cursor()
        result = await callback(cursor)
        self.connection.commit()
        return result

    async def _ensure_coin_row(self, cursor, user_id: int) -> None:
        cursor.execute(
            "INSERT OR IGNORE INTO user_coins (user_id) VALUES (?)",
            (user_id,),
        )

    async def _add_coins(
        self,
        cursor,
        user_id: int,
        amount: int,
        typ: str,
        reason: str,
        actor_id: int,
    ) -> None:
        await self._ensure_coin_row(cursor, user_id)
        cursor.execute(
            "UPDATE user_coins SET balance = balance + ? WHERE user_id = ?",
            (amount, user_id),
        )

    @staticmethod
    def _month_key() -> int:
        return 2026 * 12 + 7

    @staticmethod
    def _is_leadership_any(member) -> bool:
        return member.id == 3

    @staticmethod
    def _is_elder(member) -> bool:
        return member.id == 1


class AchievementRewardContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_cwl_uses_a_transactional_public_reward_contract(self) -> None:
        owner = _RewardOwner()
        self.addCleanup(owner.connection.close)
        service = AchievementRewardService(owner)
        members = [MagicMock(id=1), MagicMock(id=2), MagicMock(id=3)]

        result = await service.grant_cwl_rewards(
            members,
            reward_kind="coins",
            reason="cwl_bonus_BEH_1",
            actor_id=99,
        )

        self.assertEqual(result.granted_ids, (1, 2))
        self.assertEqual(result.already_granted_ids, ())
        self.assertEqual(result.elder_grants, ((1, 10),))
        self.assertEqual(result.member_grants, ((2, 5),))
        self.assertEqual(result.skipped, ((3, "leadership excluded"),))
        balances = owner.connection.execute(
            "SELECT user_id, balance FROM user_coins ORDER BY user_id"
        ).fetchall()
        self.assertEqual(balances, [(1, 10), (2, 5)])

    async def test_cwl_coin_retry_does_not_grant_twice(self) -> None:
        owner = _RewardOwner()
        self.addCleanup(owner.connection.close)
        service = AchievementRewardService(owner)
        members = [MagicMock(id=1), MagicMock(id=2)]

        first = await service.grant_cwl_rewards(
            members,
            reward_kind="coins",
            reason="cwl_bonus_BEH_1",
            actor_id=99,
        )
        second = await service.grant_cwl_rewards(
            members,
            reward_kind="coins",
            reason="cwl_bonus_BEH_1",
            actor_id=99,
        )

        self.assertEqual(first.granted_ids, (1, 2))
        self.assertEqual(second.granted_ids, ())
        self.assertEqual(second.already_granted_ids, (1, 2))
        self.assertEqual(
            second.skipped,
            ((1, "reward already granted"), (2, "reward already granted")),
        )
        balances = owner.connection.execute(
            "SELECT user_id, balance FROM user_coins ORDER BY user_id"
        ).fetchall()
        self.assertEqual(balances, [(1, 10), (2, 5)])

    async def test_cwl_ticket_contract_preserves_monthly_deduplication(
        self,
    ) -> None:
        owner = _RewardOwner()
        self.addCleanup(owner.connection.close)
        service = AchievementRewardService(owner)
        member = MagicMock(id=2)

        first = await service.grant_cwl_rewards(
            [member],
            reward_kind="ticket",
            reason="cwl_bonus_BEH_1",
            actor_id=99,
        )
        second = await service.grant_cwl_rewards(
            [member],
            reward_kind="ticket",
            reason="cwl_bonus_BEH_1",
            actor_id=99,
        )

        self.assertEqual(first.granted_ids, (2,))
        self.assertEqual(first.already_granted_ids, ())
        self.assertEqual(second.granted_ids, ())
        self.assertEqual(second.already_granted_ids, ())
        self.assertEqual(
            second.skipped,
            ((2, "User already has a ticket this month."),),
        )
