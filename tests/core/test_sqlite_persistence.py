from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from elbow_helper.features.rosters.repository import RosterRepository
from elbow_helper.features.rosters.repository.migrations import RosterSchemaError
from elbow_helper.infrastructure.persistence import SQLiteMigration
from elbow_helper.infrastructure.persistence import SQLiteMigrationError
from elbow_helper.infrastructure.persistence import UnversionedSQLiteSchemaError
from elbow_helper.infrastructure.persistence import UnsupportedSQLiteVersionError
from elbow_helper.infrastructure.persistence import run_sqlite_migrations
from elbow_helper.infrastructure.persistence import sqlite_connection
from elbow_helper.infrastructure.persistence import sqlite_schema_version
from elbow_helper.infrastructure.persistence import sqlite_transaction


class SQLiteConnectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "nested" / "state.sqlite3"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_connection_configuration_and_transaction_commit(self) -> None:
        with sqlite_connection(
            self.path,
            timeout_seconds=2,
            busy_timeout_ms=1_250,
        ) as connection:
            self.assertEqual(
                int(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
                1,
            )
            self.assertEqual(
                int(connection.execute("PRAGMA busy_timeout").fetchone()[0]),
                1_250,
            )
            self.assertEqual(
                int(connection.execute("PRAGMA synchronous").fetchone()[0]),
                1,
            )
            with sqlite_transaction(connection):
                connection.execute("CREATE TABLE state(value TEXT NOT NULL)")
                connection.execute("INSERT INTO state VALUES ('kept')")

        with closing(sqlite3.connect(self.path)) as connection:
            value = connection.execute("SELECT value FROM state").fetchone()[0]
        self.assertEqual(value, "kept")

    def test_transaction_rolls_back_every_statement(self) -> None:
        with sqlite_connection(self.path) as connection:
            connection.execute("CREATE TABLE state(value TEXT NOT NULL)")
            connection.commit()

            with self.assertRaisesRegex(RuntimeError, "stop"):
                with sqlite_transaction(connection, immediate=True):
                    connection.execute("INSERT INTO state VALUES ('discarded')")
                    raise RuntimeError("stop")

            count = int(connection.execute("SELECT COUNT(*) FROM state").fetchone()[0])
        self.assertEqual(count, 0)

    def test_foreign_keys_are_enforced(self) -> None:
        with sqlite_connection(self.path) as connection:
            connection.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
            connection.execute(
                """
                CREATE TABLE child(
                    parent_id INTEGER NOT NULL REFERENCES parent(id)
                )
                """
            )
            connection.commit()

            with self.assertRaises(sqlite3.IntegrityError):
                with sqlite_transaction(connection):
                    connection.execute("INSERT INTO child VALUES (999)")


class SQLiteMigrationTests(unittest.TestCase):
    def test_migrations_commit_schema_data_and_target_version(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)

        migrations = (
            SQLiteMigration(
                1,
                "create settings",
                lambda conn: conn.execute(
                    "CREATE TABLE settings(value TEXT NOT NULL)"
                ),
            ),
            SQLiteMigration(
                3,
                "seed settings",
                lambda conn: conn.execute(
                    "INSERT INTO settings VALUES ('ready')"
                ),
            ),
        )

        applied = run_sqlite_migrations(
            connection,
            migrations,
            target_version=3,
        )

        self.assertEqual(applied, (1, 3))
        self.assertEqual(sqlite_schema_version(connection), 3)
        self.assertEqual(
            connection.execute("SELECT value FROM settings").fetchone()[0],
            "ready",
        )

    def test_migration_failure_rolls_back_schema_data_and_version(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)

        def fail(connection: sqlite3.Connection) -> None:
            connection.execute("INSERT INTO settings VALUES ('discarded')")
            connection.execute("CREATE TABLE unfinished(id INTEGER)")
            raise RuntimeError("broken migration")

        migrations = (
            SQLiteMigration(
                1,
                "create settings",
                lambda conn: conn.execute(
                    "CREATE TABLE settings(value TEXT NOT NULL)"
                ),
            ),
            SQLiteMigration(2, "fail safely", fail),
        )

        with self.assertRaises(SQLiteMigrationError):
            run_sqlite_migrations(connection, migrations, target_version=2)

        self.assertEqual(sqlite_schema_version(connection), 0)
        objects = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        self.assertEqual(objects, [])

    def test_current_target_version_is_an_idempotent_no_op(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("PRAGMA user_version=4")
        calls = 0

        def apply(_connection: sqlite3.Connection) -> None:
            nonlocal calls
            calls += 1

        applied = run_sqlite_migrations(
            connection,
            (SQLiteMigration(4, "baseline", apply),),
            target_version=4,
        )

        self.assertEqual(applied, ())
        self.assertEqual(calls, 0)

    def test_newer_database_version_is_rejected(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("PRAGMA user_version=5")

        with self.assertRaises(UnsupportedSQLiteVersionError):
            run_sqlite_migrations(
                connection,
                (SQLiteMigration(4, "baseline", lambda conn: None),),
                target_version=4,
            )

    def test_non_empty_unversioned_database_is_rejected(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE legacy(id INTEGER)")
        connection.commit()

        with self.assertRaises(UnversionedSQLiteSchemaError):
            run_sqlite_migrations(
                connection,
                (SQLiteMigration(1, "baseline", lambda conn: None),),
                target_version=1,
            )

    def test_duplicate_or_unordered_migrations_are_rejected(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        migrations = (
            SQLiteMigration(2, "later", lambda conn: None),
            SQLiteMigration(2, "duplicate", lambda conn: None),
        )

        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            run_sqlite_migrations(connection, migrations, target_version=2)


class RosterSchemaMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "rosters.sqlite3"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_blank_database_receives_supported_v5_schema(self) -> None:
        repository = RosterRepository(self.path)

        with repository.connect() as connection:
            version = sqlite_schema_version(connection)
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }

        self.assertEqual(version, 5)
        self.assertTrue(
            {
                "rosters",
                "roster_cycles",
                "roster_members",
                "roster_automation_events",
                "roster_posts",
                "roster_layouts",
            }.issubset(tables)
        )

    def test_incomplete_versioned_schema_fails_before_repository_use(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TABLE rosters(id INTEGER PRIMARY KEY)")
            connection.execute("PRAGMA user_version=4")
            connection.commit()

        with self.assertRaises(RosterSchemaError):
            RosterRepository(self.path)
