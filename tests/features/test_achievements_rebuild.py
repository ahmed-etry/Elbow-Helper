"""Rehearse the complete achievements cleanup with the deployed DDL and synthetic rows."""

from collections import Counter
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from elbow_helper.features.achievements import schema
from elbow_helper.infrastructure.persistence.migrations import (
    SQLiteMigration, SQLiteMigrationError, UnversionedSQLiteSchemaError,
)
from elbow_helper.infrastructure.persistence.versioned import VersionedSQLiteDatabase

from .test_database_versions import quote, rewrite_table


LIVE_DDL = Path(__file__).parents[1] / "fixtures" / "achievements_live_schema.sql"


def deployed_database(path, version=0):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(LIVE_DDL.read_text(encoding="utf-8"))
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'raffle_winners_old'"
        )]
        for table in tables:
            columns = connection.execute(f"PRAGMA table_info({quote(table)})").fetchall()
            for seed in (7, 23):
                values = []
                for index, column in enumerate(columns):
                    name, kind = column[1:3]
                    if name in ("id", "user_id") and kind == "INTEGER":
                        value = seed
                    elif name in ("id", "achievement_id"):
                        value = f"achievement-{seed}"
                    elif kind == "TEXT":
                        value = f"Synthetic ' caf\u00e9 \U0001f41d {seed} {name}"
                    elif kind == "REAL":
                        value = seed + 0.125
                    else:
                        value = seed * 100 + index
                    values.append(value)
                connection.execute(
                    f"INSERT INTO {quote(table)} VALUES ({','.join('?' for _ in values)})", values,
                )
        connection.execute("UPDATE user_stats SET active_channels=NULL, message_count=NULL WHERE user_id=23")
        connection.execute("UPDATE achievements SET required_count=NULL, emoji=NULL WHERE id='achievement-23'")
        connection.execute("UPDATE sqlite_sequence SET seq=9000 WHERE name='raffle_winners'")
        connection.execute("UPDATE sqlite_sequence SET seq=5000 WHERE name='coin_transactions'")
        connection.execute(f"PRAGMA user_version={version}")


def objects(connection):
    return connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()


def values(connection):
    result = {}
    for table, in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        columns = sorted(row[1] for row in connection.execute(f"PRAGMA table_info({quote(table)})")
                         if row[1] != "leadership_only")
        result[table] = Counter(
            tuple((type(value).__name__, value) for value in row)
            for row in connection.execute(f"SELECT {','.join(map(quote, columns))} FROM {quote(table)}")
        )
    return result


def database(**overrides):
    arguments = dict(migrations=schema.DATABASE.migrations, target_version=schema.SCHEMA_VERSION,
                     validate=schema.validate_schema, after_migrate=schema.compact_database)
    arguments.update(overrides)
    return VersionedSQLiteDatabase("achievements", schema.create_schema, schema.adopt_schema, **arguments)


class AchievementsRebuildTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "achievements.db"

    def assert_fresh_schema(self, connection):
        with closing(sqlite3.connect(":memory:")) as fresh:
            schema.create_schema(fresh)
            self.assertEqual(objects(connection), objects(fresh))
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_live_shape_rebuild_keeps_every_value_and_matches_fresh_exactly(self):
        for version in (0, 1):
            with self.subTest(version=version):
                path = self.path.with_name(f"achievements-{version}.db")
                deployed_database(path, version)
                with closing(sqlite3.connect(path)) as connection:
                    before = values(connection)
                self.assertEqual(before.pop("raffle_winners_old"), Counter())
                with self.assertNoLogs("elbow_helper", level="WARNING"):
                    database().initialize(path)
                    database().initialize(path)  # a separate startup must stay quiet
                with closing(sqlite3.connect(path)) as connection:
                    self.assertEqual(values(connection), before)
                    self.assert_fresh_schema(connection)
                    self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
                    self.assertEqual(dict(connection.execute("SELECT * FROM sqlite_sequence")),
                                     {"coin_transactions": 5000, "raffle_winners": 9000})

    def test_failure_mid_copy_rolls_back_every_table_object_and_version(self):
        deployed_database(self.path)
        before = self.path.read_bytes()
        original = schema.DATABASE.migrations[-1]

        def fail(connection):
            def authorize(action, name, detail, database_name, source):
                if action == sqlite3.SQLITE_INSERT and name == "user_stats" and database_name == "main":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            connection.set_authorizer(authorize)
            original.apply(connection)

        migrations = (*schema.DATABASE.migrations[:-1], SQLiteMigration(2, original.name, fail))
        with self.assertLogs("elbow_helper.infrastructure.persistence.versioned", level="ERROR"):
            with self.assertRaises(SQLiteMigrationError):
                database(migrations=migrations).initialize(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.path.with_name(self.path.name + "-wal").exists())
        with self.assertNoLogs("elbow_helper", level="WARNING"):
            database().initialize(self.path)

    def test_compaction_runs_after_commit_once_and_preserves_rows(self):
        deployed_database(self.path)
        calls = []

        def compact(connection):
            self.assertFalse(connection.in_transaction)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assert_fresh_schema(connection)
            before = values(connection)
            schema.compact_database(connection)
            self.assertEqual(values(connection), before)
            calls.append(True)

        owner = database(after_migrate=compact)
        owner.initialize(self.path)
        owner.initialize(self.path)
        database(after_migrate=compact).initialize(self.path)
        self.assertEqual(calls, [True])

    def test_failed_compaction_keeps_committed_data_and_retries_before_opening(self):
        deployed_database(self.path)
        attempts = []

        def compact(connection):
            attempts.append(True)
            self.assertFalse(connection.in_transaction)
            if len(attempts) == 1:
                raise sqlite3.OperationalError("synthetic compaction failure")
            schema.compact_database(connection)

        owner = database(after_migrate=compact)
        with self.assertLogs("elbow_helper.infrastructure.persistence.versioned", level="ERROR"):
            with self.assertRaisesRegex(sqlite3.OperationalError, "synthetic compaction failure"):
                owner.initialize(self.path)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assert_fresh_schema(connection)
            before = values(connection)
        with closing(owner.connect(self.path)) as connection:
            self.assertEqual(values(connection), before)
        self.assertEqual(attempts, [True, True])

    def test_unplaced_rows_and_schema_objects_are_kept_and_reported(self):
        deployed_database(self.path)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("INSERT INTO raffle_winners_old VALUES (12, NULL, 345)")
            connection.execute("CREATE TABLE unknown_archive(payload BLOB)")
            connection.execute("INSERT INTO unknown_archive VALUES (x'00ff80')")
            connection.execute("CREATE INDEX unknown_coin_index ON user_coins(balance)")
            connection.execute("CREATE VIEW unknown_coin_view AS SELECT user_id FROM user_coins")
            connection.execute("CREATE TRIGGER unknown_coin_trigger AFTER DELETE ON user_coins "
                               "BEGIN INSERT INTO unknown_archive VALUES (x'11'); END")
            before = values(connection)
        with self.assertLogs(schema.LOGGER.name, level="WARNING") as logged:
            database().initialize(self.path)
        for name in ("raffle_winners_old (1 rows)", "unknown_archive (1 rows)", "unknown_coin_index",
                     "unknown_coin_view", "unknown_coin_trigger"):
            self.assertTrue(any(name in line for line in logged.output), logged.output)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(values(connection), before)
            self.assertEqual(len(connection.execute("SELECT * FROM unknown_coin_view").fetchall()), 2)
            connection.execute("DELETE FROM user_coins WHERE user_id=7")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM unknown_archive").fetchone()[0], 2)

    def test_unknown_columns_and_affinity_changes_are_refused_unchanged(self):
        for change in ("column", "affinity"):
            with self.subTest(change=change):
                path = self.path.with_name(f"{change}.db")
                deployed_database(path)
                if change == "column":
                    with closing(sqlite3.connect(path)) as connection, connection:
                        connection.execute("ALTER TABLE user_coins ADD COLUMN unknown_value BLOB")
                        connection.execute("UPDATE user_coins SET unknown_value=x'00ff80'")
                else:
                    rewrite_table(path, "user_stats", lambda sql: sql.replace(
                        "message_count INTEGER DEFAULT 0", "message_count TEXT"))
                    with closing(sqlite3.connect(path)) as connection, connection:
                        connection.execute("UPDATE user_stats SET message_count='00123'")
                before = path.read_bytes()
                with self.assertLogs("elbow_helper.infrastructure.persistence.versioned", level="ERROR"):
                    with self.assertRaises(UnversionedSQLiteSchemaError):
                        database().initialize(path)
                self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
