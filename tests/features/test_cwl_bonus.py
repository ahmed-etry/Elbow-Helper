from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
import zipfile

from elbow_helper.features.achievements.rewards import AchievementRewardService
from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.cwl.bonus.analysis import BonusAnalysisService
from elbow_helper.features.cwl.bonus.export import BonusWorkbookWriter
from elbow_helper.features.cwl.bonus.service import BonusReportError
from elbow_helper.features.cwl.bonus.service import BonusReportService
from elbow_helper.features.cwl.bonus.state import BonusDashboardStore
from elbow_helper.infrastructure.clash import ClashClient


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
            ClashClient(None),
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


class ClanHealthCwlReadTests(unittest.TestCase):
    def test_completed_war_reads_are_owned_by_the_bonus_repository(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "wars.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(
                """
                CREATE TABLE wars (
                    war_id TEXT,
                    clan_code TEXT,
                    cwl_season TEXT,
                    cwl_round INTEGER,
                    attacks_per_member INTEGER,
                    state TEXT,
                    end_ts INTEGER,
                    war_type TEXT
                );
                CREATE TABLE war_roster_members (
                    war_id TEXT,
                    clan_code TEXT,
                    player_tag TEXT,
                    player_name TEXT,
                    townhall INTEGER,
                    map_position INTEGER,
                    attacks_expected INTEGER,
                    attacks_used INTEGER,
                    roster_state TEXT
                );
                CREATE TABLE war_attacks (
                    war_id TEXT,
                    clan_code TEXT,
                    war_type TEXT,
                    player_tag TEXT,
                    player_name TEXT,
                    attack_order INTEGER,
                    defender_tag TEXT,
                    defender_townhall INTEGER,
                    stars INTEGER,
                    destruction REAL
                );
                INSERT INTO wars VALUES (
                    'CWL:#WAR', 'BEH', '2026-07', 1, 1,
                    'warEnded', 1, 'CWL'
                );
                INSERT INTO war_roster_members VALUES (
                    'CWL:#WAR', 'BEH', '#A', 'Ahmad', 18, 1, 1, 1,
                    'active'
                );
                INSERT INTO war_attacks VALUES (
                    'CWL:#WAR', 'BEH', 'CWL', '#A', 'Ahmad', 1,
                    '#D', 18, 3, 100
                );
                """
            )
        repository = ClanHealthRepository(path)

        self.assertEqual(
            repository.bonus_seasons(["BEH"]),
            ["2026-07"],
        )
        wars = repository.bonus_wars("BEH", "2026-07")
        self.assertEqual(wars[0]["roster"][0]["player_tag"], "#A")
        self.assertEqual(wars[0]["attacks"][0]["defender_tag"], "#D")


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
        exports.retention_days = 7
        exports.cleanup.return_value = (0, None)
        with tempfile.TemporaryDirectory() as temp_dir:
            exports.path_for.side_effect = (
                lambda filename: Path(temp_dir) / filename
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
            "cwl_bonus_*.xlsx"
        )
        publisher.upload_workbook.assert_awaited_once()

    async def test_invalid_season_is_classified_for_the_adapter(self) -> None:
        analysis = MagicMock()
        analysis.seasons.return_value = ["2026-07"]
        config = MagicMock()
        config.load.return_value = ({"clans": {}}, [])
        service = BonusReportService(
            analysis,
            config,
            MagicMock(),
            MagicMock(),
            MagicMock(),
        )

        with self.assertRaises(BonusReportError) as raised:
            await service.create("BEH", "2026-06")

        self.assertEqual(raised.exception.kind, "invalid_season")
        self.assertEqual(raised.exception.requested_season, "2026-06")
        self.assertEqual(
            raised.exception.available_seasons,
            ["2026-07"],
        )


class BonusWorkbookTests(unittest.TestCase):
    def test_feature_writer_produces_a_valid_xlsx_package(self) -> None:
        writer = BonusWorkbookWriter()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bonus.xlsx"
            writer.write(
                path,
                [("Summary", [["Player", "Score"], ["Ahmad", 1.5]])],
                workbook_clan_code="BEH",
            )
            with zipfile.ZipFile(path) as workbook:
                names = set(workbook.namelist())

        self.assertIn("xl/workbook.xml", names)
        self.assertIn("xl/worksheets/sheet1.xml", names)
        self.assertIn("xl/styles.xml", names)


class BonusDashboardStoreTests(unittest.TestCase):
    def test_state_and_locks_have_one_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "dashboard.json"
            store = BonusDashboardStore(path)
            store.state["boards"]["final:1"] = {"message_id": 123}
            store.save()
            reloaded = BonusDashboardStore(path)

        self.assertEqual(
            reloaded.state["boards"]["final:1"]["message_id"],
            123,
        )
        self.assertIs(store.lock("final:1"), store.lock("final:1"))


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
        self.assertEqual(result.elder_grants, ((1, 10),))
        self.assertEqual(result.member_grants, ((2, 5),))
        self.assertEqual(result.skipped, ((3, "leadership excluded"),))
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
        self.assertEqual(second.granted_ids, ())
        self.assertEqual(
            second.skipped,
            ((2, "User already has a ticket this month."),),
        )
