from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
import sqlite3
from tests.temp_directory import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

from elbow_helper.features.achievements.database import COIN_DB_INIT
from elbow_helper.features.achievements.rewards import AchievementRewardService
from elbow_helper.features.cwl.bonus.analysis import BonusAnalysisService
from elbow_helper.features.cwl.bonus.export import BonusWorkbookWriter
from elbow_helper.infrastructure.clash import ClashClient
from tests.features.profile_source import profile_demand
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

    def test_existing_cwl_coin_transactions_are_claimed_during_upgrade(
        self,
    ) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute(
            """
            CREATE TABLE coin_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                type TEXT NOT NULL,
                reason TEXT,
                actor_id INTEGER,
                created_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO coin_transactions
                (user_id, amount, type, reason, actor_id, created_at)
            VALUES (2, 5, 'bonus_cwl', 'cwl_bonus_BEH_1', 99, 1)
            """
        )

        connection.execute(
            "INSERT INTO coin_transactions (user_id, amount, type, reason, actor_id, created_at) "
            "VALUES (3, 5, 'other', 'unrelated', 99, 1)"
        )
        # Reopening an upgraded database must neither duplicate grants nor claim other awards.
        for _ in range(2):
            for statement in COIN_DB_INIT:
                connection.execute(statement)

        grants = connection.execute(
            """
            SELECT reason, user_id, reward_kind
            FROM cwl_reward_grants
            """
        ).fetchall()
        self.assertEqual(grants, [("cwl_bonus_BEH_1", 2, "coins")])

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


class BonusAnalysisTests(unittest.TestCase):
    def test_scoring_is_independent_from_the_discord_cog(self) -> None:
        repository = MagicMock()
        repository.bonus_wars.return_value = [{
            "cwl_round": 1,
            "war_id": "CWL:#WAR",
            "attacks_per_member": 1,
            "roster": [{
                "player_tag": "#A",
                "player_name": "Ahmad",
                "townhall": 18,
                "attacks_expected": 1,
                "attacks_used": 1,
            }],
            "attacks": [{
                "player_tag": "#A",
                "player_name": "Ahmad",
                "attack_order": 1,
                "defender_tag": "#D",
                "defender_townhall": 18,
                "stars": 3,
                "destruction": 100,
            }],
        }]
        service = BonusAnalysisService(
            profile_demand(ClashClient(None)),
            repository,
        )
        config = {"clans": {"BEH": {
            "matchup_expected": {"18:18": 2.0},
            "max_downhit": 2,
            "max_uphit": 8,
            "downhit_penalty_per_level": 0.15,
            "uphit_bonus_per_level": 0.10,
            "downhit_severe_after": 0,
            "downhit_severe_base": 0.20,
            "downhit_severe_multiplier": 2.0,
        }}}

        summary, ineligible, raw, warnings, errors = (
            service.analyze_clan("BEH", "2026-07", config)
        )

        self.assertEqual(ineligible, [])
        self.assertEqual(warnings, [])
        self.assertEqual(errors, [])
        self.assertEqual(summary[0]["player_tag"], "#A")
        self.assertEqual(summary[0]["avg_adjusted_delta"], 1.0)
        self.assertEqual(raw[0]["actual_score"], 3.0)

    def test_downhit_penalty_and_uphit_credit_have_exact_scores(self):
        for defender_th, adjustment, delta in ((18, 0.0, 0.5), (16, -0.9, -0.4), (20, 0.2, 0.7)):
            with self.subTest(defender_th=defender_th):
                repository = MagicMock()
                repository.bonus_wars.return_value = [{
                    "cwl_round": 1, "war_id": "CWL:#WAR", "attacks_per_member": 1,
                    "roster": [{"player_tag": "#A", "player_name": "Alpha", "townhall": 18,
                                "attacks_expected": 1, "attacks_used": 1}],
                    "attacks": [{"player_tag": "#A", "attack_order": 1, "defender_tag": "#D",
                                 "defender_townhall": defender_th, "stars": 2, "destruction": 50}],
                }]
                service = BonusAnalysisService(MagicMock(), repository)
                summary, ineligible, raw, warnings, errors = service.analyze_clan(
                    "BEH", "2026-07", {"clans": {"BEH": {
                        "matchup_expected": {f"18:{defender_th}": 2.0},
                        "downhit_penalty_per_level": 0.15, "uphit_bonus_per_level": 0.10,
                        "downhit_severe_after": 0, "downhit_severe_base": 0.20,
                        "downhit_severe_multiplier": 2.0,
                    }}},
                )
                self.assertEqual((ineligible, warnings, errors), ([], [], []))
                self.assertEqual(raw[0]["actual_score"], 2.5)
                self.assertEqual(raw[0]["base_delta"], 0.5)
                self.assertAlmostEqual(raw[0]["delta_adjustment"], adjustment)
                self.assertAlmostEqual(raw[0]["adjusted_delta"], delta)
                self.assertAlmostEqual(summary[0]["avg_adjusted_delta"], delta)
                self.assertEqual(summary[0]["total_actual"], 2.5)

    def test_failed_or_incomplete_war_evidence_never_produces_scores(self):
        repository = MagicMock()
        service = BonusAnalysisService(MagicMock(), repository)
        for failure in (sqlite3.OperationalError("unavailable"), [{"state": "inWar"}]):
            with self.subTest(failure=failure):
                repository.bonus_wars.side_effect = failure if isinstance(failure, Exception) else None
                repository.bonus_wars.return_value = failure
                logs = (self.assertLogs("elbow_helper.features.cwl.bonus.analysis", level="ERROR")
                        if isinstance(failure, Exception) else nullcontext())
                with logs:
                    summary, ineligible, raw, warnings, errors = service.analyze_clan("BEH", "2026-07", {})
                self.assertEqual((summary, ineligible, raw, warnings), ([], [], [], []))
                self.assertTrue(errors)


class BonusWorkbookWriterTests(unittest.TestCase):
    def test_workbook_preserves_sheet_names_cells_numbers_and_layout(self):
        import zipfile
        from xml.etree import ElementTree as ET
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "bonus.xlsx"
            writer = BonusWorkbookWriter()
            writer.write(path, [
                ("Guide", [["Metric", "Explanation", "Example"], ["Score", "A & B < C", "two\nlines"]]),
                ("Summary", [["Player", "Avg Final Delta/Attack"], ["Alpha", -0.4]]),
                ("Raw Attacks", [["Round", "Actual Score"], [1, 2.5], [2, 3.0]]),
            ], workbook_clan_code="BEH")
            with zipfile.ZipFile(path) as archive:
                self.assertIsNone(archive.testzip())
                for name in archive.namelist():
                    if name.endswith(".xml") or name.endswith(".rels"):
                        ET.fromstring(archive.read(name))
                workbook = ET.fromstring(archive.read("xl/workbook.xml"))
                self.assertEqual([s.attrib["name"] for s in workbook.findall("s:sheets/s:sheet", ns)],
                                 ["Guide", "Summary", "Raw Attacks"])
                guide = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
                self.assertEqual(guide.find(".//s:c[@r='B2']/s:is/s:t", ns).text, "A & B < C")
                self.assertEqual(guide.find(".//s:c[@r='C2']/s:is/s:t", ns).text, "two\nlines")
                summary = ET.fromstring(archive.read("xl/worksheets/sheet2.xml"))
                self.assertEqual(summary.find(".//s:c[@r='B2']/s:v", ns).text, "-0.4")
                self.assertEqual(summary.find("s:conditionalFormatting", ns).attrib["sqref"], "B2:B2")
                raw = ET.fromstring(archive.read("xl/worksheets/sheet3.xml"))
                self.assertEqual(raw.find("s:sheetViews/s:sheetView/s:pane", ns).attrib["state"], "frozen")
                self.assertEqual(raw.find("s:autoFilter", ns).attrib["ref"], "A1:B1")
                self.assertNotEqual(raw.find(".//s:c[@r='B2']", ns).attrib["s"],
                                    raw.find(".//s:c[@r='B3']", ns).attrib["s"])

    def test_destination_failure_is_reported(self):
        with TemporaryDirectory() as directory:
            parent = Path(directory) / "file"
            parent.write_text("occupied")
            with self.assertRaises(OSError):
                BonusWorkbookWriter().write(parent / "bonus.xlsx", [("Summary", [["Player"]])])
