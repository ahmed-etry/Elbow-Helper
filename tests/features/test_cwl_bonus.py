from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import Mock
import zipfile

from elbow_helper.features.achievements.database import COIN_DB_INIT
from elbow_helper.features.achievements.rewards import AchievementRewardService
from elbow_helper.features.cwl.bonus.analysis import BonusAnalysisService
from elbow_helper.features.cwl.bonus.export import BonusWorkbookWriter
from elbow_helper.features.cwl.bonus.service import BonusReportError
from elbow_helper.features.cwl.bonus.service import BonusReportService
from elbow_helper.features.cwl.bonus.state import BonusDashboardStore
from elbow_helper.features.cwl.dashboard import CwlDashboardMixin
from elbow_helper.infrastructure.clash import ClashClient
from elbow_helper.infrastructure.clash import ClashResponse

from tests.features.cwl_record_stub import LeagueRecord, attack, league_war, member
from tests.features.profile_source import profile_demand


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


class RecordCwlReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_war_reads_come_from_the_family_record(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        record = await LeagueRecord(temporary.name).initialize()
        await record.week("BEH", "2026-07", [league_war(
            "#C0", 1, 1783036800, [member("#P2", name="Synthetic", townhall=18, position=1)],
            [attack(1, "#P2", "#Q0", townhall=18, position=1)], team_size=15)], league="Champion League II")
        history = record.history()

        self.assertEqual(await asyncio.to_thread(history.bonus_seasons, ["BEH"]), ["2026-07"])
        wars = await asyncio.to_thread(history.bonus_wars, "BEH", "2026-07")
        self.assertEqual(wars[0]["roster"][0]["player_tag"], "#P2")
        self.assertEqual(wars[0]["attacks"][0]["defender_tag"], "#Q0")
        analysis = BonusAnalysisService(profile_demand(ClashClient(None)), history)
        summary, _, raw, _, errors = await asyncio.to_thread(
            analysis.analyze_clan, "BEH", "2026-07", {"clans": {"BEH": {"matchup_expected": {"18:18": 2.0}}}})
        self.assertEqual(errors, [])
        self.assertEqual(summary[0]["total_adjusted_delta"], 1.0)
        self.assertEqual(len(raw), 1)
        # An unreadable record is an error, never an empty season.
        broken = BonusAnalysisService(profile_demand(ClashClient(None)), SimpleNamespace(
            bonus_wars=Mock(side_effect=sqlite3.OperationalError("locked"))))
        self.assertEqual(broken.analyze_clan("BEH", "2026-07", {})[4], ["BEH: CWL data couldn't be loaded."])

    async def test_live_wars_come_from_the_record_groups(self) -> None:
        groups = SimpleNamespace(current_wars=AsyncMock(return_value=([{"_round": 1}], [])))
        analysis = BonusAnalysisService(profile_demand(ClashClient(None)), MagicMock(), groups)
        self.assertEqual(await analysis.current_wars("BEH"), ([{"_round": 1}], []))
        groups.current_wars.assert_awaited_once_with("BEH")
        self.assertEqual(await BonusAnalysisService(profile_demand(ClashClient(None)), MagicMock()).current_wars("BEH"),
                         ([], ["BEH: failed to fetch league group."]))


class DashboardProfileTests(unittest.IsolatedAsyncioTestCase):
    def dashboard(self, responses=None):
        responses = responses or {}

        async def get(path, **_options):
            return responses[path]

        client = SimpleNamespace(configured=True, get=AsyncMock(side_effect=get))
        dashboard = CwlDashboardMixin()
        dashboard.bonus_analysis = BonusAnalysisService(profile_demand(client), MagicMock())
        dashboard._hero_sum_cache = {}
        dashboard._clan_league_cache = {}
        return dashboard, client

    async def test_hero_sums_count_home_heroes_through_the_collectors(self) -> None:
        profile = {"tag": "#2PP", "heroes": [
            {"name": "Barbarian King", "level": 90, "village": "home"},
            {"name": "Archer Queen", "level": 85},
            {"name": "Battle Machine", "level": 30, "village": "builderBase"}]}
        dashboard, client = self.dashboard({
            "/players/%232PP": ClashResponse(200, profile, {}, 1, 5),
            "/players/%232PQ": ClashResponse(503, {"reason": "maintenance"}, {}, 3, 5),
        })
        self.assertEqual(await dashboard._fetch_hero_sums(["#2PP", "#2PQ"]), {"#2PP": 175, "#2PQ": None})
        self.assertEqual({call.args[0] for call in client.get.await_args_list}, {"/players/%232PP", "/players/%232PQ"})
        self.assertEqual({call.kwargs["attempts"] for call in client.get.await_args_list}, {3})
        # The dashboard keeps both answers, the failure included, as before.
        self.assertEqual(await dashboard._fetch_hero_sums(["#2PP", "#2PQ"]), {"#2PP": 175, "#2PQ": None})
        self.assertEqual(client.get.await_count, 2)

    async def test_league_names_come_from_clan_profiles_through_the_collectors(self) -> None:
        dashboard, client = self.dashboard({
            "/clans/%232PP": ClashResponse(200, {"tag": "#2PP", "warLeague": {"name": " Master League I "}}, {}, 1, 5),
            "/clans/%232PQ": ClashResponse(404, {"reason": "notFound"}, {}, 1, 5),
        })
        self.assertEqual(await dashboard._fetch_clan_war_league_name("#2PP"), "Master League I")
        self.assertIsNone(await dashboard._fetch_clan_war_league_name("#2PQ"))
        self.assertEqual([call.args[0] for call in client.get.await_args_list], ["/clans/%232PP", "/clans/%232PQ"])
        self.assertEqual(await dashboard._fetch_clan_war_league_name("#2PP"), "Master League I")
        self.assertEqual(client.get.await_count, 2)

    async def test_without_clash_access_nothing_is_asked(self) -> None:
        dashboard, client = self.dashboard()
        client.configured = False
        self.assertEqual(await dashboard._fetch_hero_sums(["#2PP"]), {"#2PP": None})
        self.assertIsNone(await dashboard._fetch_clan_war_league_name("#2PP"))
        client.get.assert_not_awaited()


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
        with tempfile.TemporaryDirectory() as temp_dir:
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


class AchievementRewardSchemaTests(unittest.TestCase):
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

        for statement in COIN_DB_INIT:
            connection.execute(statement)

        grants = connection.execute(
            """
            SELECT reason, user_id, reward_kind
            FROM cwl_reward_grants
            """
        ).fetchall()
        self.assertEqual(grants, [("cwl_bonus_BEH_1", 2, "coins")])


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
