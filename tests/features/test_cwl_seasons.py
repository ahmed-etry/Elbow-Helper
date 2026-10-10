"""Synthetic leagues keep one start-derived key and their original API labels."""

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.domain.cwl import cwl_season_key
from elbow_helper.configuration.clans import CLAN_TAGS
from elbow_helper.features.clan_health.api import ClanHealthCollector
from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.cwl.bonus.commands import CwlBonusMixin
from elbow_helper.features.cwl.transfers import CwlTransferMixin


def _war(day, *, month=10, round_number=1):
    return {"cwl_round": round_number,
            "start_ts": int(datetime(2026, month, day, 8, tzinfo=timezone.utc).timestamp())}


class CwlSeasonTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_league_uses_one_key_for_all_its_wars(self):
        collector = object.__new__(ClanHealthCollector)
        collector._fetch_coc_json_with_status = AsyncMock(return_value=(200, {
            "season": "2026-10-02", "rounds": [
                {"warTags": ["#ONE"]}, {"warTags": ["#TWO"]},
            ],
        }))
        collector._fetch_coc_json = AsyncMock(side_effect=[
            {"clan": {"tag": CLAN_TAGS["BE1"]}, "state": "warEnded",
             "startTime": "20261003T080000.000Z"},
            {"clan": {"tag": CLAN_TAGS["BE1"]}, "state": "warEnded",
             "startTime": "20261012T080000.000Z"},
        ])
        with patch("elbow_helper.features.clan_health.api.is_cwl_window", return_value=True):
            wars, _ = await collector._fetch_cwl_wars_for_clan(clan_code="BE1")
        self.assertEqual([war["_season"] for war in wars], ["2026-10", "2026-10"])
        self.assertEqual([war["_season_label"] for war in wars], ["2026-10-02", "2026-10-02"])

    def test_war_storage_preserves_the_key_and_raw_label_on_partial_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ClanHealthRepository(Path(directory) / "wars.sqlite3")
            repository.initialize()
            row = {"war_id": "CWL:#WAR", "clan_code": "BE1", "war_type": "CWL",
                   "cwl_season": "2026-06-catchup", "cwl_season_label": "2026-06-16"}
            repository.store_wars([row])
            repository.store_wars([{**row, "cwl_season_label": ""}])
            with closing(sqlite3.connect(repository.path)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT cwl_season, cwl_season_label FROM wars",
                ).fetchone(), ("2026-06-catchup", "2026-06-16"))

    def test_league_key_uses_earliest_estimated_start(self):
        self.assertEqual(cwl_season_key([_war(3), _war(2)]), "2026-10")
        self.assertEqual(cwl_season_key([_war(11), _war(10)]), "2026-10")
        self.assertEqual(cwl_season_key([_war(11)]), "2026-10-catchup")
        self.assertEqual(cwl_season_key([_war(6, round_number=5)]), "2026-10")
        self.assertEqual(cwl_season_key([_war(21, month=6, round_number=5)]),
                         "2026-06-catchup")

    def test_api_writes_keys_and_preserves_each_raw_label(self):
        collector = object.__new__(ClanHealthCollector)
        for label, day, key in (("2026-10-01", 2, "2026-10"),
                                ("2026-10-02", 3, "2026-10"),
                                ("2026-06-16", 17, "2026-06-catchup"),
                                ("2026-06-catchup", 17, "2026-06-catchup")):
            with self.subTest(label=label):
                month = int(label[5:7])
                payload = {"state": "warEnded", "clan": {"tag": "#AAA"},
                           "startTime": f"2026{month:02d}{day:02d}T080000.000Z",
                           "_season": "ignored", "_season_label": "ignored"}
                row = collector._extract_war_row(
                    war_payload=payload, clan_code="BE1", clan_tag="#AAA", war_id="CWL:#WAR",
                    war_type="CWL", source="synthetic", cwl_season=key,
                    cwl_season_label=label, cwl_round=1,
                )
                self.assertEqual(row["cwl_season"], key)
                self.assertEqual(row["cwl_season_label"], label)

    async def test_migration_merges_labels_and_is_a_noop_on_second_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ClanHealthRepository(Path(directory) / "wars.sqlite3")
            samples = [("2026-10-01", _war(2)), ("2026-10-02", _war(3)),
                       ("2026-06", _war(2, month=6)), ("2026-06-01", _war(3, month=6)),
                       ("2026-06-catchup", _war(17, month=6)),
                       ("2026-06-16", _war(21, month=6, round_number=5))]
            with closing(sqlite3.connect(repository.path)) as connection, connection:
                connection.execute("""
                    CREATE TABLE wars (
                        war_id TEXT NOT NULL,
                        war_type TEXT NOT NULL,
                        clan_code TEXT NOT NULL,
                        clan_tag TEXT NOT NULL,
                        cwl_season TEXT NOT NULL DEFAULT '',
                        cwl_round INTEGER DEFAULT 0,
                        start_ts INTEGER DEFAULT 0,
                        end_ts INTEGER DEFAULT 0,
                        state TEXT NOT NULL DEFAULT '',
                        last_seen_ts INTEGER NOT NULL,
                        source TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY (war_id, clan_code)
                    )
                """)
                connection.execute("PRAGMA user_version=23")
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
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 23)
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
            self.assertEqual(repository.bonus_seasons(["BE1"]),
                             ["2026-10", "2026-06-catchup", "2026-06"])
            workflow = CwlBonusMixin()
            workflow.bonus_reports = SimpleNamespace(available_seasons=AsyncMock(
                return_value=repository.bonus_seasons(["BE1"]),
            ))
            choices = await workflow.cwl_bonus_season_autocomplete(
                SimpleNamespace(namespace=SimpleNamespace(clan="BE1")), "2026-10",
            )
            self.assertEqual([choice.value for choice in choices], ["2026-10"])

    def test_fresh_database_stores_cwl_wars_without_the_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = ClanHealthRepository(Path(directory) / "wars.sqlite3")
            with patch("elbow_helper.features.clan_health.database.schema.key_stored_cwl_seasons"):
                repository.initialize()
            repository.store_wars([{
                "war_id": "CWL:#FRESH", "clan_code": "BE1", "war_type": "CWL",
                "cwl_season": "2026-10", "cwl_season_label": "2026-10-02",
            }])
            with closing(sqlite3.connect(repository.path)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT cwl_season, cwl_season_label FROM wars",
                ).fetchone(), ("2026-10", "2026-10-02"))
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)

    async def test_transfer_statuses_use_one_start_check_and_the_league_key(self):
        workflow = CwlTransferMixin()
        workflow.clash_client = SimpleNamespace(configured=True, get=AsyncMock())
        workflow._get_league_wars = AsyncMock()
        started = {"season": "2026-10-02", "state": "inWar"}
        cases = [
            (started, [_war(3)], ({"BE1"}, set())),
            (started, [], (set(), {"BE1"})),
            (started, [_war(17)], (set(), set())),
            (started, [_war(3, month=9)], (set(), set())),
            ({"state": "searching", "rounds": [{"warTags": ["#0"]}]},
             [], (set(), set())),
            ({"state": "unknown", "rounds": [{"warTags": ["#WAR"]}]},
             [_war(3)], ({"BE1"}, set())),
        ]
        for group, wars, expected in cases:
            with self.subTest(group=group, wars=wars):
                workflow.clash_client.get.return_value = SimpleNamespace(
                    status=200, payload_object=group,
                )
                workflow._get_league_wars.reset_mock()
                workflow._get_league_wars.return_value = wars
                self.assertEqual(await workflow._cwl_spin_statuses(
                    {"BE1"}, datetime(2026, 10, 4, tzinfo=timezone.utc),
                ), expected)
                if group["state"] == "searching":
                    workflow._get_league_wars.assert_not_awaited()
                else:
                    workflow._get_league_wars.assert_awaited_once_with("BE1")
