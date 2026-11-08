from __future__ import annotations

import asyncio
from tests.temp_directory import TemporaryDirectory
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import AsyncMock
from unittest.mock import patch
from xml.etree import ElementTree

from elbow_helper.features.cwl.roster.analysis import build_ass_season_metrics
from elbow_helper.features.cwl.roster.analysis import build_mega_ass_metrics
from elbow_helper.features.cwl.roster.analysis import CwlRosterAnalysisMixin
from elbow_helper.features.cwl.roster.export import CwlRosterExportMixin
from elbow_helper.features.cwl.roster.models import ASS_PROFILE_HIGH
from elbow_helper.features.cwl.roster.models import ASS_PROFILE_LOWER
from elbow_helper.infrastructure.exports import ExportColumn as RosterColumn
from elbow_helper.infrastructure.exports import ExportSheet as RosterSheet
from elbow_helper.infrastructure.exports import WorkbookWriter

from tests.features.cwl_record_stub import LeagueRecord, attack, league_war, member


class _WorkbookHarness(CwlRosterExportMixin):
    workbook_writer = WorkbookWriter()


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
            },
            {
                "player_tag": "#B",
                "player_name_last_seen": "Beta",
            },
            {
                "player_tag": "#C",
                "player_name_last_seen": "Gamma",
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


def _synthetic_position_war(team_size: int) -> tuple[list[dict], list[dict], list[dict]]:
    war_id = f"position-war-{team_size}"
    wars = [{
        "war_id": war_id,
        "clan_code": "BE4",
        "cwl_season": "2026-06",
        "cwl_league": "Master League I",
        "team_size": team_size,
        "attacks_per_member": 1,
        "end_ts": team_size,
    }]
    roster = []
    attacks = []
    for position in range(1, team_size + 1):
        identity = {
            "war_id": war_id,
            "clan_code": "BE4",
            "player_tag": f"#P{position}",
        }
        roster.append({
            **identity,
            "player_name": f"Player {position}",
            "townhall": 18,
            "map_position": position,
            "attacks_expected": 1,
        })
        attacks.append({
            **identity,
            "stars": 3,
            "destruction": 100,
            "defender_map_position": position,
        })
    return wars, roster, attacks


class AssAnalysisTests(unittest.TestCase):
    def test_target_position_scale_tracks_war_size_for_both_profiles(self) -> None:
        for profile, weight in ((ASS_PROFILE_HIGH, 0.4), (ASS_PROFILE_LOWER, 1.0)):
            self.assertEqual(profile.difficulty_weight, weight)
            for team_size in (10, 15, 20, 30, 45):
                with self.subTest(profile=profile.key, team_size=team_size):
                    wars, roster, attacks = _synthetic_position_war(team_size)
                    metrics = build_ass_season_metrics(
                        wars=wars, roster=roster, attacks=attacks,
                        season_order={"2026-06": 1},
                        profiles_by_clan={"BE4": profile},
                    )
                    by_tag = {metric.player_tag: metric for metric in metrics}
                    step = weight * 15 / team_size
                    top = by_tag["#P1"]
                    bottom = by_tag[f"#P{team_size}"]
                    expected_top = (team_size - 1) / 2 * step
                    self.assertAlmostEqual(top.difficulty_adjustment, expected_top)
                    self.assertAlmostEqual(bottom.difficulty_adjustment, -expected_top)
                    self.assertAlmostEqual(top.score, 21 + expected_top)
                    self.assertAlmostEqual(bottom.score, 21 - expected_top)
                    for position in range(1, team_size):
                        higher = by_tag[f"#P{position}"]
                        lower = by_tag[f"#P{position + 1}"]
                        self.assertAlmostEqual(
                            higher.difficulty_adjustment - lower.difficulty_adjustment,
                            step,
                        )
                    if team_size % 2:
                        middle = by_tag[f"#P{(team_size + 1) // 2}"]
                        self.assertEqual(middle.difficulty_adjustment, 0)

    def test_mixed_war_sizes_are_scaled_before_averaging(self) -> None:
        wars, roster, attacks = [], [], []
        for team_size in (15, 30):
            war_rows, roster_rows, attack_rows = _synthetic_position_war(team_size)
            wars.extend(war_rows)
            roster.extend(roster_rows)
            attacks.extend(attack_rows)
        metrics = build_ass_season_metrics(
            wars=wars, roster=roster, attacks=attacks,
            season_order={"2026-06": 1},
            profiles_by_clan={"BE4": ASS_PROFILE_HIGH},
        )
        top = next(metric for metric in metrics if metric.player_tag == "#P1")
        self.assertEqual(top.attacks, 2)
        self.assertAlmostEqual(top.difficulty_adjustment, 2.85)
        self.assertAlmostEqual(top.score, 23.85)
        self.assertEqual(top.average_target_position, 1)

    def test_missing_war_size_uses_roster_positions_for_scaling(self) -> None:
        for missing_size in (None, 0, -1):
            with self.subTest(team_size=missing_size):
                wars, roster, attacks = _synthetic_position_war(30)
                wars[0]["team_size"] = missing_size
                metrics = build_ass_season_metrics(
                    wars=wars, roster=roster, attacks=attacks,
                    season_order={"2026-06": 1},
                    profiles_by_clan={"BE4": ASS_PROFILE_HIGH},
                )
                top = next(metric for metric in metrics if metric.player_tag == "#P1")
                self.assertAlmostEqual(top.difficulty_adjustment, 2.9)
                self.assertAlmostEqual(top.score, 23.9)

    def test_missing_positions_and_war_size_do_not_prevent_scoring(self) -> None:
        wars, roster, attacks = _synthetic_position_war(1)
        wars[0].pop("team_size")
        roster[0].pop("map_position")
        attacks[0].pop("defender_map_position")
        metrics = build_ass_season_metrics(
            wars=wars, roster=roster, attacks=attacks,
            season_order={"2026-06": 1},
            profiles_by_clan={"BE4": ASS_PROFILE_HIGH},
        )
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0].difficulty_adjustment, 0)
        self.assertEqual(metrics[0].score, 21)


    @staticmethod
    def _season(prefix: str, first_end: int) -> list[dict]:
        return [league_war(f"#{prefix}{'0289PYL'[day - 1]}", day, first_end + day * 86400,
                           [member("#P8", name="Alpha", townhall=18, position=1)],
                           [attack(1, "#P8", "#Q0", position=1)], team_size=15) for day in range(1, 8)]


    def test_db_loader_ignores_incomplete_seasons(self) -> None:
        with TemporaryDirectory() as directory:
            record = asyncio.run(LeagueRecord(directory).initialize())
            asyncio.run(record.week("BE4", "2026-07", self._season("C", 1782000000)[:1],
                                    league="Master League I"))
            dataset = record.history().roster_history(3)

        self.assertEqual(dataset["seasons"], [])
        self.assertEqual(dataset["wars"], [])


class CandidateTests(unittest.IsolatedAsyncioTestCase):
    async def test_attachment_cleanup_warning_does_not_discard_the_workbook(self):
        path = SimpleNamespace(read_bytes=MagicMock(return_value=b"synthetic workbook"))
        harness = SimpleNamespace(
            prepare_cwl_roster_workbook=AsyncMock(return_value=("timestamp", "test.xlsx", path)),
            cwl_exports=SimpleNamespace(delete=MagicMock(return_value="Synthetic cleanup warning")),
        )
        with self.assertLogs("elbow_helper.features.cwl.roster.export", level="WARNING") as logs:
            result = await CwlRosterExportMixin.build_cwl_roster_attachment(harness, ())
        self.assertEqual(result, ("test.xlsx", b"synthetic workbook"))
        self.assertTrue(any("Synthetic cleanup warning" in line for line in logs.output))


    async def test_closed_signup_roster_uses_only_its_selected_accounts(
        self,
    ) -> None:
        member = _Member(1, "Leader")
        with patch.object(
            CwlRosterAnalysisMixin,
            "_load_directory_players",
            return_value={},
        ):
            result = await _AnalysisHarness()._build_roster_candidates(
                guild=_Guild([member]),
                season_metrics=[],
                mega_metrics=[],
            )

        self.assertNotIn("records", result)
        self.assertNotIn("links_by_user", result)
        self.assertTrue(all("cwl_records" not in row for row in result["candidates"]))
        self.assertEqual(result["signed_member_count"], 1)
        self.assertEqual(result["signed_account_count"], 2)
        self.assertEqual(
            {row["player_tag"] for row in result["candidates"]},
            {"#A", "#B"},
        )


class WorkbookTests(unittest.TestCase):
    def test_30v30_workbook_exports_scaled_ass_values(self) -> None:
        wars, roster, attacks = _synthetic_position_war(30)
        metrics = build_ass_season_metrics(
            wars=wars, roster=roster, attacks=attacks,
            season_order={"2026-06": 1},
            profiles_by_clan={"BE4": ASS_PROFILE_HIGH},
        )
        top = next(metric for metric in metrics if metric.player_tag == "#P1")
        mega = next(
            metric for metric in build_mega_ass_metrics(metrics)
            if metric.player_tag == "#P1"
        )
        sheets = _WorkbookHarness()._build_roster_workbook(
            candidates=[{
                "discord_member": "Member",
                "account_name": top.player_name,
                "player_tag": top.player_tag,
                "townhall": top.townhall,
                "current_clan": "BE4",
                "latest": top,
                "mega": mega,
            }],
            signed_tags={top.player_tag},
            season_metrics=metrics,
            seasons=[{"key": "2026-06"}],
            profiles={"BE4": ASS_PROFILE_HIGH},
            latest_leagues={"BE4": "Master League I"},
            clan_choices=("BE4",),
        )
        planner, history = sheets[:2]
        self.assertEqual(planner.rows[0][7], 23.9)
        self.assertEqual(planner.rows[0][11], 23.9)
        self.assertEqual(history.rows[0][6], 23.9)
        self.assertEqual(history.rows[0][16], 2.9)

        namespace = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "ass-30v30.xlsx"
            _WorkbookHarness()._write_roster_xlsx(path, sheets)
            with zipfile.ZipFile(path) as workbook:
                for sheet_index, expected_cells in (
                    (1, {"H2": 23.9, "L2": 23.9}),
                    (2, {"G2": 23.9, "Q2": 2.9}),
                ):
                    root = ElementTree.fromstring(workbook.read(
                        f"xl/worksheets/sheet{sheet_index}.xml",
                    ))
                    for cell, expected in expected_cells.items():
                        with self.subTest(sheet=sheet_index, cell=cell):
                            value = root.findtext(
                                f".//m:c[@r='{cell}']/m:v", namespaces=namespace,
                            )
                            self.assertEqual(float(value), expected)


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
        with TemporaryDirectory() as directory:
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
