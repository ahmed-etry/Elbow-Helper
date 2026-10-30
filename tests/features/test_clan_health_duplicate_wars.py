"""Delayed regular wars must contribute exactly one complete snapshot."""

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.features.clan_health.api import ClanHealthCollector
from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.clan_health.database import schema
from elbow_helper.features.clan_health.database.migrations import deduplicate_regular_wars
from elbow_helper.infrastructure.persistence.migrations import SQLiteMigrationError, run_sqlite_migrations


def snapshot(end=300, *, prep=100, start=200, state="inWar", seen=400,
             used=1, clan="#CLAN", opponent="#OPP", code="TEST", war_id=None, war_type="REG"):
    war_id = war_id or f"REG:{clan}:{end}:{opponent}"
    war = dict(war_id=war_id, war_type=war_type, clan_code=code, clan_tag=clan,
               opponent_tag=opponent, preparation_start_ts=prep, start_ts=start,
               end_ts=end, state=state, last_seen_ts=seen, team_size=1, attacks_per_member=2)
    member = dict(war_id=war_id, clan_code=code, player_tag="#PLAYER", player_name="Synthetic",
                  attacks_expected=2, attacks_used=used)
    roster = [{**member, "roster_state": state, "captured_ts": seen}] if state != "preparation" else []
    activity = [{**member, "war_type": war_type, "clan_tag": clan, "end_ts": end, "attack_count": used}]
    attacks = [{**activity[0], "attack_order": n, "war_state": state, "defender_tag": "#DEFENDER"}
               for n in range(1, used + 1)]
    return war, roster, activity, attacks


class DuplicateRegularWarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "health.db"
        self.repo = ClanHealthRepository(self.path)
        self.repo.initialize()

    def rows(self, table):
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute(f'SELECT * FROM "{table}" ORDER BY 1, 2').fetchall()

    def state(self):
        return {table: self.rows(table) for table in (
            "wars", "war_activity", "war_attacks", "war_roster_members",
        )}

    def legacy_store(self, data):
        war, roster, activity, attacks = data
        self.repo.store_wars([war])
        self.repo.store_final_war_rosters(roster)
        self.repo.store_war_activity(activity)
        self.repo.store_war_attacks(attacks)

    def upgrade(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return run_sqlite_migrations(conn, schema.DATABASE.migrations,
                                         target_version=schema.SCHEMA_VERSION)

    def downgrade(self, version=1):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(f"PRAGMA user_version={version}")

    def assert_one_complete(self, end):
        self.assertEqual(len(self.rows("wars")), 1)
        self.assertEqual(self.rows("wars")[0][0], f"REG:#CLAN:{end}:#OPP")
        self.assertEqual(len(self.rows("war_attacks")), 2)
        self.assertEqual(len(self.rows("war_roster_members")), 1)
        activity = self.repo.war_activity(cycle_start_ts=0, cycle_end_ts=1000)["#PLAYER"]
        self.assertEqual(activity["regular_war_events_joined"], 1)
        self.assertEqual(activity["attacks_expected"], 2)
        self.assertEqual(activity["attacks_used"], 2)
        self.assertEqual(activity["attacks_expected"] - activity["attacks_used"], 0)
        self.assertEqual(self.repo.clan_war_counts(cycle_start_ts=0, cycle_end_ts=1000),
                         {"TEST": {"regular": 1, "cwl": 0}})

    def test_midwar_end_shift_replaces_all_facts_and_counts_once(self):
        self.repo.store_regular_war_snapshot(*snapshot())
        self.repo.store_regular_war_snapshot(*snapshot(360, state="warEnded", seen=500, used=2))
        self.assert_one_complete(360)

    def test_preparation_start_stable_when_battle_start_and_end_shift(self):
        self.repo.store_regular_war_snapshot(*snapshot(state="preparation", used=0))
        self.repo.store_regular_war_snapshot(*snapshot(360, start=260, state="warEnded", seen=500, used=2))
        self.assert_one_complete(360)

    def test_different_preparations_clans_opponents_and_unknown_identity_stay_separate(self):
        variants = [snapshot(), snapshot(360, prep=110), snapshot(370, clan="#OTHER", code="OTHER"),
                    snapshot(380, opponent="#OTHER"), snapshot(390, prep=0), snapshot(400, prep=0),
                    snapshot(410, opponent=""), snapshot(420, opponent="")]
        for data in variants:
            self.repo.store_regular_war_snapshot(*data)
        self.downgrade()
        self.upgrade()
        self.assertEqual(len(self.rows("wars")), len(variants))

    def test_tagged_regular_wars_and_cwl_are_not_merged(self):
        for war_type, prefix in (("REG", "REG"), ("CWL", "CWL")):
            for tag in ("#WARONE", "#WARTWO"):
                self.legacy_store(snapshot(war_id=f"{prefix}:{tag}", war_type=war_type))
        before = self.state()
        self.repo.store_regular_war_snapshot(*snapshot())
        self.repo.store_regular_war_snapshot(*snapshot(360, seen=500))
        self.downgrade()
        self.upgrade()
        for table, rows in before.items():
            self.assertEqual([row for row in self.rows(table) if row[0] in {r[0] for r in rows}], rows)

    def test_stale_snapshot_cannot_resurrect_duplicate_or_overwrite_same_id(self):
        current = snapshot(360, state="warEnded", seen=500, used=2)
        self.repo.store_regular_war_snapshot(*current)
        before = self.state()
        for old in (snapshot(seen=400), snapshot(360, seen=400), snapshot(seen=500)):
            self.assertEqual(self.repo.store_regular_war_snapshot(*old), 0)
            self.assertEqual(self.state(), before)

    def test_live_keeps_newest_snapshot_even_if_older_copy_is_ended(self):
        self.repo.store_regular_war_snapshot(*snapshot(state="warEnded", seen=400))
        self.repo.store_regular_war_snapshot(*snapshot(360, seen=500, used=2))
        self.assert_one_complete(360)

    def test_write_failure_rolls_back_superseded_deletion_and_new_facts(self):
        self.repo.store_regular_war_snapshot(*snapshot())
        before = self.state()
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("CREATE TRIGGER fail_attack BEFORE INSERT ON war_attacks "
                         "BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic write failure"):
            self.repo.store_regular_war_snapshot(*snapshot(360, seen=500, used=2))
        self.assertEqual(self.state(), before)

    def test_missing_war_row_writes_nothing(self):
        war, roster, activity, attacks = snapshot()
        for invalid in (None, {}, {**war, "war_id": ""}, {**war, "clan_code": ""}):
            with self.subTest(war=invalid):
                self.assertEqual(self.repo.store_regular_war_snapshot(invalid, roster, activity, attacks), 0)
                self.assertTrue(all(not rows for rows in self.state().values()))

    def test_existing_invalid_row_writes_still_skip_database_creation(self):
        repo = ClanHealthRepository(Path(self.temp.name) / "unused.db")
        self.assertEqual(repo.store_wars([{"clan_code": "TEST"}]), 0)
        self.assertEqual(repo.store_final_war_rosters([{"war_id": "REG:#INVALID"}]), 0)
        self.assertFalse(repo.path.exists())

    def test_upgrade_prefers_ended_else_latest_and_runs_once(self):
        self.legacy_store(snapshot(state="warEnded", seen=400, used=2))
        self.legacy_store(snapshot(360, seen=500))
        self.legacy_store(snapshot(600, prep=450, seen=700))
        self.legacy_store(snapshot(660, prep=450, seen=800, used=2))
        self.legacy_store(snapshot(900, prep=750, state="preparation", seen=950, used=0))
        self.downgrade()
        with self.assertLogs("elbow_helper.features.clan_health.database.migrations", level="INFO") as logs:
            self.assertEqual(self.upgrade(), (2,))
        self.assertIn("wars_removed=2 attack_rows_removed=2", logs.output[-1])
        self.assertEqual({row[0] for row in self.rows("wars")}, {
            "REG:#CLAN:300:#OPP", "REG:#CLAN:660:#OPP", "REG:#CLAN:900:#OPP",
        })
        before = self.state()
        self.assertEqual(self.upgrade(), ())
        self.assertEqual(self.state(), before)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            deduplicate_regular_wars(conn)
        self.assertEqual(self.state(), before)
        aggregate = self.repo.war_activity(cycle_start_ts=0, cycle_end_ts=700)["#PLAYER"]
        self.assertEqual(aggregate["regular_war_events_joined"], 2)
        self.assertEqual(aggregate["attacks_expected"], 4)
        self.assertEqual(aggregate["attacks_used"], 4)

    def test_unversioned_backup_also_receives_cleanup(self):
        self.legacy_store(snapshot())
        self.legacy_store(snapshot(360, state="warEnded", seen=500, used=2))
        self.downgrade(0)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(run_sqlite_migrations(
                conn, schema.DATABASE.migrations, target_version=schema.SCHEMA_VERSION,
                adopt_unversioned=schema.adopt_schema,
            ), (1, 2))
        self.assert_one_complete(360)

    def test_upgrade_failure_rolls_back_deletions_and_version_stamp(self):
        self.legacy_store(snapshot())
        self.legacy_store(snapshot(360, state="warEnded", seen=500, used=2))
        self.downgrade()
        before = self.state()
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("CREATE TRIGGER fail_war_delete BEFORE DELETE ON wars "
                         "BEGIN SELECT RAISE(ABORT, 'synthetic migration failure'); END")
        with self.assertRaises(SQLiteMigrationError):
            self.upgrade()
        self.assertEqual(self.state(), before)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_all_war_keyed_tables_cleaned_with_clan_scope(self):
        old = snapshot()
        self.repo.store_regular_war_snapshot(*old)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('CREATE TABLE "extra facts" (war_id TEXT, clan_code TEXT, value TEXT)')
            conn.executemany('INSERT INTO "extra facts" VALUES (?, ?, ?)', [
                (old[0]["war_id"], "TEST", "superseded"),
                (old[0]["war_id"], "OTHER", "keep"),
            ])
        self.repo.store_regular_war_snapshot(*snapshot(360, seen=500))
        self.assertEqual(self.rows("extra facts"), [(old[0]["war_id"], "OTHER", "keep")])


class RegularWarIngestTests(unittest.IsolatedAsyncioTestCase):
    async def test_poll_and_scheduled_capture_use_same_atomic_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = ClanHealthRepository(Path(directory) / "health.db")
            client = MagicMock(configured=True)
            collector = ClanHealthCollector(client, repo, MagicMock())
            payload = {
                "state": "inWar", "preparationStartTime": "20260101T000000.000Z",
                "startTime": "20260102T000000.000Z", "endTime": "20260103T000000.000Z",
                "teamSize": 1, "attacksPerMember": 2,
                "clan": {"tag": "#CLAN", "members": [{"tag": "#PLAYER", "name": "Synthetic",
                    "mapPosition": 1, "attacks": [{"order": 1, "defenderTag": "#DEFENDER"}]}]},
                "opponent": {"tag": "#OPP", "members": []},
            }
            ended = deepcopy(payload)
            ended["state"] = "warEnded"
            ended["endTime"] = "20260103T010000.000Z"
            ended["clan"]["members"][0]["attacks"].append({"order": 2, "defenderTag": "#DEFENDER"})
            with (
                patch("elbow_helper.features.clan_health.api.CLAN_TAGS", {"TEST": "#CLAN"}),
                patch.object(collector, "_fetch_coc_json", new=AsyncMock(side_effect=[payload, ended])),
                patch.object(collector, "_schedule_war_end_capture"),
            ):
                await collector._ingest_regular_war_for_clan(clan_code="TEST")
                await collector._run_war_end_capture(clan_code="TEST", expected_end_ts=1)
            with closing(sqlite3.connect(repo.path)) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM wars").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM war_attacks").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT state FROM wars").fetchone()[0], "warEnded")
