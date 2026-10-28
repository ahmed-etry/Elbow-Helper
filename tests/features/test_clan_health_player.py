from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from elbow_helper.features.clan_health.analysis import ClanHealthAnalyzer
from elbow_helper.features.clan_health.config import UTC
from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.clan_health.database.schema import create_schema
from elbow_helper.features.clan_health.commands.player import ClanHealthPlayerCommandMixin


class PlayerMovementHistoryTests(unittest.TestCase):
    def test_zero_lookback_uses_only_the_selected_period(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "clan_health.db"
            with sqlite3.connect(database_path) as connection:
                create_schema(connection)
                connection.executemany(
                    "INSERT INTO player_snapshots(player_tag, captured_ts, clan_code, player_name) VALUES (?, ?, ?, ?)",
                    [
                        ("#PLAYER", 1_700_000_000, "OLD", "Player"),
                        ("#PLAYER", 1_700_100_000, "CURRENT", "Player"),
                        ("#PLAYER", 1_700_200_000, "CURRENT", "Player"),
                    ],
                )
            connection.close()

            segments = ClanHealthRepository(
                database_path
            ).player_movement(
                player_tag="#PLAYER",
                cycle_start=datetime.fromtimestamp(
                    1_700_100_000,
                    tz=UTC,
                ),
                cycle_end=datetime.fromtimestamp(
                    1_700_200_000,
                    tz=UTC,
                ),
                lookback_days=0,
            )

        self.assertEqual([segment["clan_code"] for segment in segments], ["CURRENT"])


class ClanHealthAnalyzerFacadeTests(unittest.TestCase):
    def test_sparse_report_check_keeps_static_calling_contract(self) -> None:
        analyzer = ClanHealthAnalyzer(
            ClanHealthRepository(Path("unused-clan-health.db"))
        )

        self.assertTrue(analyzer.report_row_is_sparse(None))
        self.assertFalse(
            analyzer.report_row_is_sparse({"donations": 1})
        )


class FamilyWarParticipationTests(unittest.TestCase):
    def test_home_and_extra_wars_do_not_share_a_home_only_denominator(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ClanHealthRepository(Path(directory) / "health.sqlite3")
            repository.initialize()
            repository.store_war_activity([
                {"war_id": f"war-{index}", "war_type": "REG", "clan_code": clan,
                 "clan_tag": "#P2", "end_ts": 100 + index, "player_tag": "#P0",
                 "player_name": "Synthetic", "attacks_expected": 2, "attacks_used": 2}
                for index, clan in enumerate(["BES"] * 5 + ["BEW"] * 10)
            ])
            analyzer = ClanHealthAnalyzer(repository)
            rows = [{"player_tag": "#P0", "player_name": "Synthetic", "clan_code": "BES"}]
            start, end = (datetime.fromtimestamp(value, tz=UTC) for value in (1, 200))
            counts = repository.clan_war_counts(cycle_start_ts=1, cycle_end_ts=200)
            self.assertEqual(counts["BES"]["regular"], 5)
            self.assertEqual(counts["BEW"]["regular"], 10)
            analyzer._apply_family_war_activity(rows, cycle_start=start, cycle_end=end)
            rules = {"_profile_name": "competitive", "war": {
                "wars_to_join": 5, "missed_attack_rate_percent": 0,
            }}
            with patch("elbow_helper.features.clan_health.analysis.flags.effective_player_rules",
                       return_value=rules):
                analyzer._apply_flags(rows, cycle_start=start, cycle_end=end)
            row = rows[0]
            self.assertEqual(row["regular_war_events_joined"], 15)
            self.assertEqual((row["regular_war_hits_expected"], row["regular_war_hits_used"]), (30, 30))
            card = row["signal_cards"]["war_attendance"]
            expected = "Joined 15 regular wars across the family"
            self.assertEqual(card["current"], expected)
            self.assertEqual(ClanHealthPlayerCommandMixin._signal_summary_text(card), expected)
            self.assertEqual(card["verdict"], "Good")
            self.assertEqual(row["war_hit_usage_verdict"], "Good")

    def test_one_and_no_regular_wars_have_matching_report_wording(self):
        from types import SimpleNamespace

        repository = SimpleNamespace(clan_war_counts=lambda **_: {})
        rules = {"_profile_name": "casual", "war": {"wars_to_join": 0}}
        for count, expected in ((1, "Joined 1 regular war across the family"),
                                (0, "No regular wars joined during this period")):
            with self.subTest(count=count), patch(
                "elbow_helper.features.clan_health.analysis.flags.effective_player_rules", return_value=rules,
            ):
                rows = [{"player_tag": "#P0", "clan_code": "BES", "regular_war_events_joined": count}]
                ClanHealthAnalyzer(repository)._apply_flags(rows)
                card = rows[0]["signal_cards"]["war_attendance"]
                self.assertEqual(card["current"], expected)
                self.assertEqual(card["reason"], "Regular wars are not required for this clan.")
