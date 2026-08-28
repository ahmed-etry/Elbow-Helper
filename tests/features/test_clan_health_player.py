from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from elbow_helper.features.clan_health.analysis import ClanHealthAnalyzer
from elbow_helper.features.clan_health.config import UTC
from elbow_helper.features.clan_health.database import ClanHealthRepository


class PlayerMovementHistoryTests(unittest.TestCase):
    def test_zero_lookback_uses_only_the_selected_period(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "clan_health.db"
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "CREATE TABLE player_snapshots (player_tag TEXT, captured_ts INTEGER, clan_code TEXT, player_name TEXT)"
                )
                connection.executemany(
                    "INSERT INTO player_snapshots VALUES (?, ?, ?, ?)",
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
