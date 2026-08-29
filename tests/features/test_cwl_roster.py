from __future__ import annotations

import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.cwl.roster.analysis import build_ass_season_metrics
from elbow_helper.features.cwl.roster.analysis import build_mega_ass_metrics
from elbow_helper.features.cwl.roster.analysis import CwlRosterAnalysisMixin
from elbow_helper.features.cwl.roster.export import CwlRosterExportMixin
from elbow_helper.features.cwl.roster.models import AssSeasonMetric
from elbow_helper.features.cwl.roster.models import ASS_PROFILE_HIGH
from elbow_helper.features.cwl.roster.models import ASS_PROFILE_LOWER
from elbow_helper.features.cwl.roster.models import MegaAssMetric
from elbow_helper.infrastructure.exports import ExportColumn as RosterColumn
from elbow_helper.infrastructure.exports import ExportSheet as RosterSheet
from elbow_helper.infrastructure.exports import GoogleSheetsPublisher
from elbow_helper.infrastructure.exports import WorkbookWriter
from elbow_helper.infrastructure.exports.google_sheets import GOOGLE_EXPORT_OWNER_KEY
from elbow_helper.infrastructure.exports.google_sheets import GOOGLE_EXPORT_OWNER_VALUE
from elbow_helper.infrastructure.exports.google_sheets import HEADER_HEIGHT_PX
from elbow_helper.features.cwl.roster.models import profile_for_league


class _WorkbookHarness(CwlRosterExportMixin):
    workbook_writer = WorkbookWriter()


class RosterSheetFormattingTests(unittest.TestCase):
    def test_wrapped_google_sheet_headers_have_room_for_two_lines(self) -> None:
        self.assertGreaterEqual(HEADER_HEIGHT_PX, 44)

    def test_formatted_google_sheet_is_created_and_marked_for_cleanup(self) -> None:
        sheet = RosterSheet(
            title="Roster",
            columns=(RosterColumn("Account", 150),),
            rows=(("Ahmad",),),
            tab_color="3B5B92",
        )
        spreadsheets = MagicMock()
        spreadsheets.create.return_value.execute.return_value = {
            "spreadsheetId": "new-sheet",
            "spreadsheetUrl": (
                "https://docs.google.com/spreadsheets/d/new-sheet/edit"
            ),
        }
        spreadsheets.batchUpdate.return_value.execute.return_value = {}
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value = spreadsheets
        drive_service = MagicMock()
        drive_service.files.return_value.list.return_value.execute.return_value = {}

        with (
            patch("google.oauth2.credentials.Credentials"),
            patch("google.auth.transport.requests.Request"),
            patch(
                "googleapiclient.discovery.build",
                side_effect=[sheets_service, drive_service],
            ),
        ):
            link, warning = GoogleSheetsPublisher(
                client_id="client",
                client_secret="secret",
                refresh_token="refresh",
                folder_id=None,
            ).create_spreadsheet_sync(
                sheets=[sheet],
                sheet_title="CWL Sign-up [Roster]",
            )

        self.assertEqual(
            link,
            "https://docs.google.com/spreadsheets/d/new-sheet/edit",
        )
        self.assertIsNone(warning)
        spreadsheets.create.assert_called_once()
        update = spreadsheets.batchUpdate.call_args.kwargs
        self.assertEqual(update["spreadsheetId"], "new-sheet")
        requests = update["body"]["requests"]
        write = next(
            request["updateCells"]
            for request in requests
            if "updateCells" in request and "rows" in request["updateCells"]
        )
        self.assertEqual(write["start"]["sheetId"], 0)
        drive_update = drive_service.files.return_value.update.call_args.kwargs
        self.assertEqual(
            drive_update["body"]["appProperties"],
            {GOOGLE_EXPORT_OWNER_KEY: GOOGLE_EXPORT_OWNER_VALUE},
        )


class _Member:
    def __init__(self, member_id: int, display_name: str) -> None:
        self.id = member_id
        self.display_name = display_name
        self.bot = False


class _Guild:
    def __init__(self, members: list[_Member]) -> None:
        self.members = {member.id: member for member in members}

    def get_member(self, member_id: int) -> _Member | None:
        return self.members.get(member_id)


class _Links:
    _player_locations = {
        "#A": {"clan_code": "BE4"},
        "#B": {"clan_code": "BES"},
        "#C": {"clan_code": "BE1"},
    }

    @staticmethod
    def get_links_for_user(_member_id: int) -> list[dict]:
        return [
            {
                "player_tag": "#A",
                "player_name_last_seen": "Alpha",
                "is_primary": 1,
            },
            {
                "player_tag": "#B",
                "player_name_last_seen": "Beta",
                "is_primary": 0,
            },
            {
                "player_tag": "#C",
                "player_name_last_seen": "Gamma",
                "is_primary": 0,
            },
        ]

    @classmethod
    def get_player_location(
        cls,
        player_tag: str,
    ) -> dict | None:
        location = cls._player_locations.get(player_tag)
        return dict(location) if location else None


class _RosterRepository:
    @staticmethod
    def get_roster(roster_id: int) -> SimpleNamespace | None:
        if roster_id != 1:
            return None
        return SimpleNamespace(
            id=1,
            active_cycle_id=7,
            status="closed",
        )

    @staticmethod
    def list_members(roster_id: int, cycle_id: int) -> list[SimpleNamespace]:
        if (roster_id, cycle_id) != (1, 7):
            return []
        return [
            SimpleNamespace(
                discord_user_id=1,
                player_tag="#A",
                player_name="Alpha",
                clan_code="BE4",
                townhall=18,
            ),
            SimpleNamespace(
                discord_user_id=1,
                player_tag="#B",
                player_name="Beta",
                clan_code="BES",
                townhall=17,
            ),
        ]


class _RosterQueries:
    @staticmethod
    async def get(roster_id: int) -> SimpleNamespace | None:
        return _RosterRepository.get_roster(roster_id)

    @staticmethod
    async def members(roster: SimpleNamespace) -> list[SimpleNamespace]:
        return _RosterRepository.list_members(roster.id, roster.active_cycle_id)


class _Bot:
    @staticmethod
    def get_cog(name: str) -> _Links | None:
        if name == "AccountLinks":
            return _Links()
        return None


class _AnalysisHarness(CwlRosterAnalysisMixin):
    bot = _Bot()
    roster_queries = _RosterQueries()
    account_links = _Links()


def _synthetic_history() -> tuple[list[dict], list[dict], list[dict]]:
    wars: list[dict] = []
    roster: list[dict] = []
    attacks: list[dict] = []
    for season_index, season in enumerate(("2026-05", "2026-06"), start=1):
        for day in range(1, 8):
            war_id = f"{season}-{day}"
            wars.append(
                {
                    "war_id": war_id,
                    "clan_code": "BE4",
                    "cwl_season": season,
                    "cwl_league": "Master League I",
                    "team_size": 15,
                    "attacks_per_member": 1,
                    "end_ts": (season_index * 100) + day,
                }
            )
            for position, tag, name in (
                (1, "#A", "Alpha"),
                (2, "#B", "Beta"),
                (3, "#ZERO", "Zero"),
            ):
                roster.append(
                    {
                        "war_id": war_id,
                        "clan_code": "BE4",
                        "player_tag": tag,
                        "player_name": name,
                        "townhall": 18,
                        "map_position": position,
                        "attacks_expected": 1,
                    }
                )
            attacks.append(
                {
                    "war_id": war_id,
                    "clan_code": "BE4",
                    "player_tag": "#A",
                    "stars": 3,
                    "destruction": 100,
                    "defender_map_position": 1,
                }
            )
            attacks.append(
                {
                    "war_id": war_id,
                    "clan_code": "BE4",
                    "player_tag": "#B",
                    "stars": 2 if season == "2026-05" else 3,
                    "destruction": 90 if season == "2026-05" else 100,
                    "defender_map_position": 8,
                }
            )
    return wars, roster, attacks


class AssProfileTests(unittest.TestCase):
    def test_latest_profile_mapping(self) -> None:
        self.assertIs(profile_for_league("Champion League III"), ASS_PROFILE_HIGH)
        self.assertIs(profile_for_league("Master League I"), ASS_PROFILE_HIGH)
        self.assertIs(profile_for_league("Master League II"), ASS_PROFILE_LOWER)
        self.assertIs(profile_for_league("Master League III"), ASS_PROFILE_LOWER)

    def test_missed_star_profiles(self) -> None:
        self.assertEqual(ASS_PROFILE_HIGH.missed_adjustment(1), 0)
        self.assertEqual(ASS_PROFILE_HIGH.missed_adjustment(2), -2)
        self.assertEqual(ASS_PROFILE_HIGH.missed_adjustment(3), -4)
        self.assertEqual(ASS_PROFILE_LOWER.missed_adjustment(2), 0)
        self.assertEqual(ASS_PROFILE_LOWER.missed_adjustment(3), -3)


class AssAnalysisTests(unittest.TestCase):
    def test_season_and_mega_ranking(self) -> None:
        wars, roster, attacks = _synthetic_history()
        season_metrics = build_ass_season_metrics(
            wars=wars,
            roster=roster,
            attacks=attacks,
            season_order={"2026-05": 1, "2026-06": 2},
            profiles_by_clan={"BE4": ASS_PROFILE_HIGH},
        )
        by_key = {
            (metric.season, metric.player_tag): metric
            for metric in season_metrics
        }

        alpha_may = by_key[("2026-05", "#A")]
        beta_may = by_key[("2026-05", "#B")]
        zero_may = by_key[("2026-05", "#ZERO")]
        self.assertAlmostEqual(alpha_may.score or 0, 23.8)
        self.assertEqual(alpha_may.rank_label, "1/2")
        self.assertEqual(beta_may.rank_label, "2/2")
        self.assertIsNone(zero_may.score)
        self.assertEqual(zero_may.rank_label, "-")
        self.assertEqual(zero_may.attacks_label, "0/7")

        mega = {
            metric.player_tag: metric
            for metric in build_mega_ass_metrics(season_metrics)
        }
        self.assertEqual(mega["#A"].rank_label, "1/2")
        self.assertEqual(mega["#A"].total_attacks, 14)
        self.assertEqual(len(mega["#A"].seasons), 2)
        self.assertNotIn("#ZERO", mega)

    def test_db_loader_reads_completed_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clan_health.db"
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript(
                    """
                    CREATE TABLE wars (
                        war_id TEXT,
                        war_type TEXT,
                        clan_code TEXT,
                        cwl_season TEXT,
                        cwl_league TEXT,
                        cwl_round INTEGER,
                        team_size INTEGER,
                        attacks_per_member INTEGER,
                        state TEXT,
                        end_ts INTEGER
                    );
                    CREATE TABLE war_roster_members (
                        war_id TEXT,
                        clan_code TEXT,
                        player_tag TEXT,
                        player_name TEXT,
                        townhall INTEGER,
                        map_position INTEGER,
                        attacks_expected INTEGER
                    );
                    CREATE TABLE war_attacks (
                        war_id TEXT,
                        war_type TEXT,
                        clan_code TEXT,
                        player_tag TEXT,
                        player_name TEXT,
                        attack_order INTEGER,
                        defender_map_position INTEGER,
                        stars INTEGER,
                        destruction REAL
                    );
                    """
                )
                for day in range(1, 8):
                    war_id = f"war-{day}"
                    conn.execute(
                        """
                        INSERT INTO wars VALUES (
                            ?, 'CWL', 'BE4', '2026-06', 'Master League I',
                            ?, 15, 1, 'warEnded', ?
                        )
                        """,
                        (war_id, day, 100 + day),
                    )
                    conn.execute(
                        """
                        INSERT INTO war_roster_members VALUES (
                            ?, 'BE4', '#A', 'Alpha', 18, 1, 1
                        )
                        """,
                        (war_id,),
                    )
                    conn.execute(
                        """
                        INSERT INTO war_attacks VALUES (
                            ?, 'CWL', 'BE4', '#A', 'Alpha', 1, 1, 3, 100
                        )
                        """,
                        (war_id,),
                    )
                conn.commit()
            dataset = ClanHealthRepository(path).roster_history(3)

        self.assertEqual([row["key"] for row in dataset["seasons"]], ["2026-06"])
        self.assertEqual(len(dataset["wars"]), 7)
        self.assertEqual(len(dataset["roster"]), 7)
        self.assertEqual(len(dataset["attacks"]), 7)

    def test_db_loader_ignores_incomplete_seasons(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clan_health.db"
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript(
                    """
                    CREATE TABLE wars (
                        war_id TEXT,
                        war_type TEXT,
                        clan_code TEXT,
                        cwl_season TEXT,
                        cwl_league TEXT,
                        cwl_round INTEGER,
                        team_size INTEGER,
                        attacks_per_member INTEGER,
                        state TEXT,
                        end_ts INTEGER
                    );
                    CREATE TABLE war_roster_members (
                        war_id TEXT,
                        clan_code TEXT,
                        player_tag TEXT
                    );
                    CREATE TABLE war_attacks (
                        war_id TEXT,
                        war_type TEXT,
                        clan_code TEXT
                    );
                    INSERT INTO wars VALUES (
                        'war-1', 'CWL', 'BE4', '2026-07', 'Master League I',
                        1, 15, 1, 'warEnded', 100
                    );
                    """
                )
                conn.commit()
            dataset = ClanHealthRepository(path).roster_history(3)

        self.assertEqual(dataset["seasons"], [])
        self.assertEqual(dataset["wars"], [])


class CandidateTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_signup_roster_uses_only_its_selected_accounts(
        self,
    ) -> None:
        member = _Member(1, "Leader")
        with patch.object(
            CwlRosterAnalysisMixin,
            "_load_directory_players",
            return_value={},
        ), patch.object(
            CwlRosterAnalysisMixin,
            "_load_active_roster_records",
            return_value=[],
        ):
            result = await _AnalysisHarness()._build_roster_candidates(
                guild=_Guild([member]),
                season_metrics=[],
                mega_metrics=[],
            )

        self.assertEqual(result["signed_member_count"], 1)
        self.assertEqual(result["signed_account_count"], 2)
        self.assertEqual(
            {row["player_tag"] for row in result["candidates"]},
            {"#A", "#B"},
        )


class WorkbookTests(unittest.TestCase):
    def test_roster_workbook_hides_non_signup_clutter(self) -> None:
        signed_metric = AssSeasonMetric(
            season="2026-06",
            season_order=1,
            latest_end_ts=200,
            clan_code="BES",
            league="Master League I",
            profile=ASS_PROFILE_HIGH,
            player_tag="#A",
            player_name="Alpha",
            townhall=18,
            attacks_expected=7,
            attacks=7,
            stars=21,
            score=21.0,
            average_defensive_position=4.0,
            rank=1,
            rank_total=2,
        )
        non_signup_metric = AssSeasonMetric(
            season="2026-06",
            season_order=1,
            latest_end_ts=200,
            clan_code="BES",
            league="Master League I",
            profile=ASS_PROFILE_HIGH,
            player_tag="#NS",
            player_name="Non Signup",
            townhall=18,
            attacks_expected=7,
            attacks=7,
            stars=18,
            score=18.0,
            average_defensive_position=8.0,
            rank=2,
            rank_total=2,
        )
        mega = MegaAssMetric(
            clan_code="BES",
            profile=ASS_PROFILE_HIGH,
            player_tag="#A",
            player_name="Alpha",
            townhall=18,
            seasons=(signed_metric,),
            score=21.0,
            total_attacks=7,
            average_defensive_position=4.0,
            rank=1,
            rank_total=2,
        )

        sheets = _WorkbookHarness()._build_roster_workbook(
            candidates=[
                {
                    "discord_member": "Leader",
                    "account_name": "Alpha",
                    "player_tag": "#A",
                    "townhall": 18,
                    "current_clan": "BE4",
                    "latest": signed_metric,
                    "mega": mega,
                    "cwl_records": 1,
                }
            ],
            signed_tags={"#A"},
            season_metrics=[signed_metric, non_signup_metric],
            records=[],
            links_by_user={},
            seasons=[{"key": "2026-06"}],
            profiles={"BES": ASS_PROFILE_HIGH},
            latest_leagues={"BES": "Master League I"},
            clan_choices=("BE4", "BES"),
        )

        self.assertEqual(
            [sheet.title for sheet in sheets],
            ["Roster Planner", "Season History", "Leadership Records", "Guide"],
        )
        planner = next(sheet for sheet in sheets if sheet.title == "Roster Planner")
        self.assertEqual(
            [column.name for column in planner.columns[:4]],
            ["Discord Member", "Account", "Player Tag", "TH"],
        )
        self.assertEqual(planner.rows[0][2], "#A")
        all_headers = {
            column.name
            for sheet in sheets
            for column in sheet.columns
        }
        self.assertNotIn("Comparison Clan", all_headers)
        self.assertNotIn("Score Trend", all_headers)
        self.assertNotIn("Rank Trend", all_headers)

        history = next(sheet for sheet in sheets if sheet.title == "Season History")
        self.assertEqual(len(history.rows), 1)
        self.assertIn("Alpha", history.rows[0])
        self.assertNotIn("Non Signup", history.rows[0])

    def test_assignment_dropdown_is_written_to_xlsx(self) -> None:
        sheet = RosterSheet(
            title="Roster Planner",
            columns=(
                RosterColumn("Account", 150),
                RosterColumn("Assigned Clan", 110),
            ),
            rows=(("Alpha", ""), ("Beta", "")),
            tab_color="3B5B92",
            dropdowns=((1, ("BEH", "BE4", "BES")),),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "planner.xlsx"
            _WorkbookHarness()._write_roster_xlsx(path, [sheet])
            with zipfile.ZipFile(path, "r") as workbook:
                worksheet = workbook.read(
                    "xl/worksheets/sheet1.xml"
                ).decode("utf-8")

        self.assertIn("dataValidations", worksheet)
        self.assertIn('sqref="B2:B3"', worksheet)
        self.assertIn('"BEH,BE4,BES"', worksheet)
        self.assertIn('rgb="FF3B5B92"', worksheet)


if __name__ == "__main__":
    unittest.main()
