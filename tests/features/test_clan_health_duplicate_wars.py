"""Old backups upgrade with one copy of each delayed regular war."""

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

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
        war_id = f"REG:#CLAN:{end}:#OPP"
        self.assertEqual([row[0] for row in self.rows("wars")], [war_id])
        self.assertEqual(len(self.rows("war_attacks")), 2)
        self.assertEqual(len(self.rows("war_roster_members")), 1)
        (activity,) = self.rows("war_activity")
        self.assertEqual((activity[0], activity[7], activity[8]), (war_id, 2, 2))

    def test_different_preparations_clans_opponents_and_unknown_identity_stay_separate(self):
        variants = [snapshot(), snapshot(360, prep=110), snapshot(370, clan="#OTHER", code="OTHER"),
                    snapshot(380, opponent="#OTHER"), snapshot(390, prep=0), snapshot(400, prep=0),
                    snapshot(410, opponent=""), snapshot(420, opponent="")]
        for data in variants:
            self.legacy_store(data)
        self.downgrade()
        self.upgrade()
        self.assertEqual(len(self.rows("wars")), len(variants))

    def test_tagged_regular_wars_and_cwl_are_not_merged(self):
        for war_type, prefix in (("REG", "REG"), ("CWL", "CWL")):
            for tag in ("#WARONE", "#WARTWO"):
                self.legacy_store(snapshot(war_id=f"{prefix}:{tag}", war_type=war_type))
        before = self.state()
        self.legacy_store(snapshot())
        self.legacy_store(snapshot(360, seen=500))
        self.downgrade()
        self.upgrade()
        for table, rows in before.items():
            self.assertEqual([row for row in self.rows(table) if row[0] in {r[0] for r in rows}], rows)

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
        self.assertEqual(sorted((row[0], row[7], row[8]) for row in self.rows("war_activity")),
                         [("REG:#CLAN:300:#OPP", 2, 2), ("REG:#CLAN:660:#OPP", 2, 2), ("REG:#CLAN:900:#OPP", 2, 0)])

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
        self.legacy_store(old)
        self.legacy_store(snapshot(360, state="warEnded", seen=500, used=2))
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('CREATE TABLE "extra facts" (war_id TEXT, clan_code TEXT, value TEXT)')
            conn.executemany('INSERT INTO "extra facts" VALUES (?, ?, ?)', [
                (old[0]["war_id"], "TEST", "superseded"),
                (old[0]["war_id"], "OTHER", "keep"),
            ])
        self.downgrade()
        self.upgrade()
        self.assertEqual(self.rows("extra facts"), [(old[0]["war_id"], "OTHER", "keep")])


if __name__ == "__main__":
    unittest.main()
