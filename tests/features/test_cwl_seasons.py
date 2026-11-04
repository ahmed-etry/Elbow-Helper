"""One league a month: its key is the month; the old store's stored keys stay readable as history.

Keys of weeks in the family record are covered in ``test_cwl_record``.
"""

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.domain import cwl
from elbow_helper.domain.cwl import SEASON_KEY
from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.clan_health.database.schema import SCHEMA_VERSION, create_schema
from elbow_helper.features.cwl.bonus.commands import CwlBonusMixin


def _war(day, *, month=10, round_number=1):
    return {"cwl_round": round_number,
            "start_ts": int(datetime(2026, month, day, 8, tzinfo=timezone.utc).timestamp())}


class CwlSeasonTests(unittest.IsolatedAsyncioTestCase):
    def test_season_keys_are_a_month_or_a_key_stored_with_a_suffix(self):
        for key in ("2026-10", "2026-06-catchup"):
            self.assertIsNotNone(re.fullmatch(SEASON_KEY, key), key)
        for key in ("2026-13", "2026-6", "2026-06-", "2026-06-Catchup", "2026-06 catchup", "June"):
            self.assertIsNone(re.fullmatch(SEASON_KEY, key), key)
        self.assertFalse(hasattr(cwl, "cwl_season_key"))

    async def test_migration_merges_labels_and_is_a_noop_on_second_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ClanHealthRepository(Path(directory) / "wars.sqlite3")
            samples = [("2026-10-01", _war(2)), ("2026-10-02", _war(3)),
                       ("2026-06", _war(2, month=6)), ("2026-06-01", _war(3, month=6)),
                       ("2026-06-catchup", _war(17, month=6)),
                       ("2026-06-16", _war(21, month=6, round_number=5))]
            with closing(sqlite3.connect(repository.path)) as connection, connection:
                create_schema(connection)
                connection.execute("ALTER TABLE wars DROP COLUMN cwl_season_label")
                connection.executemany(
                    "INSERT INTO wars (war_id, war_type, clan_code, clan_tag, cwl_season, "
                    "start_ts, cwl_round, state, last_seen_ts, source) "
                    "VALUES (?, 'CWL', 'BE1', '', ?, ?, ?, 'warEnded', 1, 'synthetic')",
                    [(f"CWL:#{index}", label, war["start_ts"], war["cwl_round"])
                     for index, (label, war) in enumerate(samples)],
                )
                before = connection.execute(
                    "SELECT war_id, clan_code, start_ts, cwl_round, state, source FROM wars",
                ).fetchall()
            with self.assertLogs("elbow_helper.features.clan_health.database.migrations", "INFO"):
                repository.initialize()
            with closing(sqlite3.connect(repository.path)) as connection:
                after = connection.execute(
                    "SELECT war_id, clan_code, start_ts, cwl_round, state, source FROM wars",
                ).fetchall()
                self.assertEqual(before, after)
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                rows = connection.execute(
                    "SELECT cwl_season, cwl_season_label FROM wars ORDER BY war_id",
                ).fetchall()
                self.assertEqual(rows, [("2026-10", "2026-10-01"), ("2026-10", "2026-10-02"),
                                        ("2026-06", "2026-06"), ("2026-06", "2026-06-01"),
                                        ("2026-06-catchup", "2026-06-catchup"),
                                        ("2026-06-catchup", "2026-06-16")])
            with self.assertNoLogs("elbow_helper.features.clan_health.database.migrations", "INFO"):
                repository.initialize()
            with closing(sqlite3.connect(repository.path)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT cwl_season, cwl_season_label FROM wars ORDER BY war_id",
                ).fetchall(), rows)
            workflow = CwlBonusMixin()
            workflow.bonus_reports = SimpleNamespace(available_seasons=AsyncMock(
                return_value=sorted({key for key, _ in rows}, reverse=True),
            ))
            choices = await workflow.cwl_bonus_season_autocomplete(
                SimpleNamespace(namespace=SimpleNamespace(clan="BE1")), "2026-10",
            )
            self.assertEqual([choice.value for choice in choices], ["2026-10"])

    def test_fresh_database_has_the_label_column_without_the_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ClanHealthRepository(Path(directory) / "wars.sqlite3")
            with patch("elbow_helper.features.clan_health.database.schema.key_stored_cwl_seasons"):
                repository.initialize()
            with closing(sqlite3.connect(repository.path)) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(wars)")}
                self.assertIn("cwl_season_label", columns)
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
