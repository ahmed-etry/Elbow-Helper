"""Adopt deployed schemas without losing data or accepting unknown formats."""

from collections import Counter
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
import sqlite3
from tests.temp_directory import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.account_links.cog import AccountLinks
from elbow_helper.features.achievements import schema as achievements_schema
from elbow_helper.features.achievements.database import AchievementsDatabaseMixin
from elbow_helper.features.achievements.cog import Achievements
from elbow_helper.infrastructure.persistence.migrations import (
    SQLiteMigration, SQLiteMigrationError, UnversionedSQLiteSchemaError, UnsupportedSQLiteVersionError,
)


# Only achievements still adopts deployed, unversioned shapes.
SCHEMAS = (achievements_schema,)


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def deployed_fixture(path, schema):
    """Run the deployed creation statements directly, before version stamping."""
    with closing(sqlite3.connect(path)) as connection, connection:
        schema.create_schema(connection)
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        for table in tables:
            columns = connection.execute(f"PRAGMA table_info({quote(table)})").fetchall()
            values = [7 if row[2] == "INTEGER" else 1.25 if row[2] == "REAL"
                      else "Synthetic ' account \u00e9\U0001f41d" for row in columns]
            connection.execute(
                f"INSERT INTO {quote(table)} VALUES ({','.join('?' for _ in values)})", values,
            )
        connection.execute("CREATE TABLE extra_data (value BLOB, nullable TEXT)")
        connection.execute("INSERT INTO extra_data VALUES (?, NULL)", (b"\x00\xff\x80",))
        connection.execute("CREATE INDEX extra_index ON extra_data(value)")
        connection.execute("CREATE TRIGGER extra_trigger BEFORE DELETE ON extra_data BEGIN SELECT 1; END")


def saved_state(path):
    with closing(sqlite3.connect(path)) as connection:
        return tuple(connection.iterdump())


def saved_values(path):
    with closing(sqlite3.connect(path)) as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        result = {}
        for table in tables:
            columns = sorted(row[1] for row in connection.execute(f"PRAGMA table_info({quote(table)})"))
            names = ','.join(map(quote, columns))
            result[table] = Counter(connection.execute(f"SELECT {names} FROM {quote(table)}").fetchall())
        return result


def version(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("PRAGMA user_version").fetchone()[0]


def rewrite_table(path, table, change, *, extra_values=None):
    """Vary one deployed declaration while retaining its rows and other objects."""
    with closing(sqlite3.connect(path)) as connection, connection:
        sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
        altered = change(sql)
        if altered == sql:
            raise AssertionError(f"Fixture did not change {table}")
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quote(table)})")]
        rows = connection.execute(f"SELECT * FROM {quote(table)}").fetchall()
        objects = connection.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index', 'trigger') AND sql IS NOT NULL",
            (table,),
        ).fetchall()
        connection.execute(f"DROP TABLE {quote(table)}")
        connection.execute(altered)
        extras = extra_values or {}
        names = columns + list(extras)
        connection.executemany(
            f"INSERT INTO {quote(table)} ({','.join(map(quote, names))}) VALUES ({','.join('?' for _ in names)})",
            [(*row, *extras.values()) for row in rows],
        )
        for statement, in objects:
            connection.execute(statement)


def live_achievements_fixture(path, *, schema_version=1, extra_flag=True, old_order=True):
    deployed_fixture(path, achievements_schema)
    if extra_flag:
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("ALTER TABLE achievements ADD COLUMN leadership_only INTEGER DEFAULT 0")
            connection.execute("UPDATE achievements SET leadership_only=1")
    if old_order:
        def reorder(sql):
            # Columns added by historical ALTER TABLE statements followed the
            # original counters, rather than appearing beside voice_hours.
            seconds = ("voice_total_seconds INTEGER DEFAULT 0,", "silent_voice_seconds INTEGER DEFAULT 0,")
            for column in seconds:
                sql = sql.replace(column, "")
            return sql.replace("night_owl_last_local_day INTEGER DEFAULT 0", (
                "night_owl_last_local_day INTEGER DEFAULT 0, " + " ".join(seconds).rstrip(",")
            ))

        rewrite_table(path, "user_stats", reorder)
    with closing(sqlite3.connect(path)) as connection, connection:
        columns = connection.execute("PRAGMA table_info(user_stats)").fetchall()
        for user_id in (7, 23, 999):
            values = [user_id if row[1] == "user_id" else
                      "12 34 caf\u00e9 \U0001f41d" if row[2] == "TEXT" else
                      user_id + 0.25 if row[2] == "REAL" else
                      user_id * 100 + index for index, row in enumerate(columns)]
            connection.execute(
                f"INSERT OR REPLACE INTO user_stats VALUES ({','.join('?' for _ in values)})", values,
            )
        connection.execute("UPDATE user_stats SET active_channels=NULL, message_count=NULL WHERE user_id=999")
        connection.execute("INSERT INTO achievements(id, name, description) VALUES ('nullable', 'Caf\u00e9 \U0001f41d', '')")
        connection.execute("INSERT INTO user_achievements VALUES (23, 'nullable', 123456)")
        connection.execute(f"PRAGMA user_version={schema_version}")


class DatabaseVersionTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def path(self, schema, suffix=""):
        return self.root / f"{schema.DATABASE.name}{suffix}.sqlite3"

    def test_blank_databases_start_at_current_version(self):
        for schema in SCHEMAS:
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                schema.DATABASE.initialize(path)
                self.assertEqual(version(path), schema.SCHEMA_VERSION)
                with closing(sqlite3.connect(path)) as connection:
                    self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")


    def assert_adopted_with_warnings(self, schema, path, messages):
        if schema is achievements_schema:
            # Version two now normalises the entire schema, rather than keeping
            # harmless declaration differences and warning on every startup.
            before = saved_values(path)
            with self.assertNoLogs("elbow_helper.infrastructure.persistence.versioned", level="WARNING"):
                schema.DATABASE.initialize(path)
                schema.DATABASE.initialize(path)
            self.assertEqual(version(path), schema.SCHEMA_VERSION)
            self.assertEqual(saved_values(path), before)
            return
        before = saved_state(path)
        with self.assertLogs("elbow_helper.infrastructure.persistence.versioned", level="WARNING") as logged:
            schema.DATABASE.initialize(path)
            schema.DATABASE.initialize(path)
        self.assertEqual(version(path), schema.SCHEMA_VERSION)
        self.assertEqual(saved_state(path), before)
        self.assertEqual(len(logged.output), len(messages), logged.output)
        for message in messages:
            self.assertTrue(any(message in line for line in logged.output), logged.output)


    def test_newer_versions_are_refused_before_any_writes(self):
        for schema in SCHEMAS:
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                deployed_fixture(path, schema)
                with closing(sqlite3.connect(path)) as connection:
                    connection.execute(f"PRAGMA user_version={schema.SCHEMA_VERSION + 1}")
                self.assert_refused_unchanged(schema, path, UnsupportedSQLiteVersionError)

    def assert_refused_unchanged(self, schema, path, error):
        before = path.read_bytes()
        with self.assertLogs("elbow_helper.infrastructure.persistence.versioned", level="ERROR") as logged:
            with self.assertRaises(error):
                schema.DATABASE.initialize(path)
        self.assertIn(schema.DATABASE.name, logged.output[0])
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(path.with_name(path.name + "-wal").exists())


    def test_achievement_cleanup_preserves_rows_and_removes_startup_warnings(self):
        for schema_version in (0, 1):
            for extra_flag, old_order in ((True, True), (True, False), (False, True)):
                with self.subTest(version=schema_version, flag=extra_flag, order=old_order):
                    path = self.path(achievements_schema, f"{schema_version}{extra_flag}{old_order}")
                    live_achievements_fixture(path, schema_version=schema_version,
                                              extra_flag=extra_flag, old_order=old_order)
                    with closing(sqlite3.connect(path)) as connection:
                        with self.assertLogs("elbow_helper.infrastructure.persistence.versioned", level="WARNING") as logged:
                            achievements_schema.validate_schema(connection)
                        expected = []
                        if extra_flag:
                            expected.append("achievements.leadership_only: extra column")
                        if old_order:
                            expected.append("user_stats: column order differs; queries name columns")
                        self.assertEqual(len(logged.output), len(expected))
                        for warning in expected:
                            self.assertTrue(any(warning in line for line in logged.output), logged.output)
                        tables = [row[0] for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )]
                        # Compare named values, so a column-order mistake cannot
                        # hide behind equal row counts or repeated sample values.
                        names = {table: [row[1] for row in connection.execute(f"PRAGMA table_info({quote(table)})")
                                         if row[1] != "leadership_only"] for table in tables}

                        def rows(database):
                            return {table: database.execute(
                                f"SELECT {','.join(map(quote, columns))} FROM {quote(table)} ORDER BY rowid"
                            ).fetchall() for table, columns in names.items()}

                        before = rows(connection)
                    # Initialize again with a fresh startup cache, like a restart.
                    for _ in range(2):
                        with patch.object(achievements_schema.DATABASE, "_ready", set()), self.assertNoLogs(
                            "elbow_helper.infrastructure.persistence.versioned", level="WARNING"
                        ):
                            achievements_schema.DATABASE.initialize(path)
                    self.assertEqual(version(path), achievements_schema.SCHEMA_VERSION)
                    with closing(sqlite3.connect(path)) as connection, closing(sqlite3.connect(":memory:")) as fresh:
                        achievements_schema.create_schema(fresh)
                        self.assertEqual(rows(connection), before)
                        for table in ("achievements", "user_stats", "user_achievements", "coin_transactions"):
                            for pragma in ("table_info", "index_list", "foreign_key_list"):
                                self.assertEqual(connection.execute(f"PRAGMA {pragma}({quote(table)})").fetchall(),
                                                 fresh.execute(f"PRAGMA {pragma}({quote(table)})").fetchall())
                        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")


    def test_achievement_cleanup_failure_rolls_back_both_tables_and_version(self):
        for schema_version in (0, 1):
            for failure in ("copy_stats", "after_cleanup"):
                with self.subTest(version=schema_version, failure=failure):
                    path = self.path(achievements_schema, f"{schema_version}{failure}")
                    live_achievements_fixture(path, schema_version=schema_version)
                    original = achievements_schema.DATABASE.migrations[-1]

                    def fail(connection):
                        self.assertTrue(connection.in_transaction)
                        if failure == "copy_stats":
                            def authorize(action, name, detail, database, source):
                                if action == sqlite3.SQLITE_INSERT and name == "user_stats" and database == "main":
                                    return sqlite3.SQLITE_DENY
                                return sqlite3.SQLITE_OK
                            connection.set_authorizer(authorize)
                        original.apply(connection)
                        raise sqlite3.OperationalError("synthetic failure after both rebuilds")

                    migrations = (*achievements_schema.DATABASE.migrations[:-1],
                                  SQLiteMigration(original.version, original.name, fail))
                    with patch.object(achievements_schema.DATABASE, "migrations", migrations):
                        self.assert_refused_unchanged(achievements_schema, path, SQLiteMigrationError)
                    # A failed upgrade must remain retryable.
                    achievements_schema.DATABASE.initialize(path)
                    self.assertEqual(version(path), achievements_schema.SCHEMA_VERSION)

    def test_achievement_cleanup_refuses_to_discard_unknown_values(self):
        for table in ("achievements", "user_stats"):
            with self.subTest(table=table):
                path = self.path(achievements_schema, table)
                live_achievements_fixture(path)
                with closing(sqlite3.connect(path)) as connection, connection:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN unknown_value BLOB")
                    connection.execute(f"UPDATE {table} SET unknown_value=x'00ff80'")
                self.assert_refused_unchanged(achievements_schema, path, UnversionedSQLiteSchemaError)

    def test_failure_after_legacy_upgrade_rolls_back_every_database(self):
        for schema in SCHEMAS:
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                deployed_fixture(path, schema)
                before = path.read_bytes()
                original = schema.DATABASE.adopt

                def failing_adoption(connection):
                    original(connection)
                    connection.execute("UPDATE extra_data SET value=x'1122'")
                    connection.execute("ALTER TABLE extra_data ADD COLUMN failed TEXT")
                    raise sqlite3.OperationalError("synthetic upgrade failure")

                with patch.object(schema.DATABASE, "adopt", failing_adoption):
                    self.assert_refused_unchanged(schema, path, SQLiteMigrationError)
                self.assertEqual(path.read_bytes(), before)
                schema.DATABASE.initialize(path)
                self.assertEqual(version(path), schema.SCHEMA_VERSION)

    def test_concurrent_first_connections_adopt_only_once(self):
        for schema in SCHEMAS:
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                deployed_fixture(path, schema)
                with patch.object(schema.DATABASE, "adopt", wraps=schema.DATABASE.adopt) as adopt:
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        list(pool.map(schema.DATABASE.initialize, [path] * 8))
                    adopt.assert_called_once()
                self.assertEqual(version(path), schema.SCHEMA_VERSION)

    def test_connections_never_recreate_a_file_removed_after_startup(self):
        for schema in SCHEMAS:
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                schema.DATABASE.initialize(path)
                path.unlink()
                with self.assertRaises(sqlite3.OperationalError):
                    schema.DATABASE.connect(path)
                self.assertFalse(path.exists())


    def test_ordinary_connections_cannot_create_or_write_an_unknown_schema(self):
        for schema in SCHEMAS:
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                with closing(sqlite3.connect(path)) as connection:
                    connection.execute("CREATE TABLE unknown(value)")
                before = path.read_bytes()
                with self.assertLogs("elbow_helper.infrastructure.persistence.versioned", level="ERROR"):
                    with self.assertRaises(UnversionedSQLiteSchemaError):
                        self.open_through_feature(schema, path)
                self.assertEqual(path.read_bytes(), before)

    def open_through_feature(self, schema, path):
        owner = AchievementsDatabaseMixin()
        owner.db_path = path
        owner.logger = logging.getLogger(__name__)
        owner.ALL_ACHIEVEMENTS = ()
        owner.init_achievements()


class DatabaseStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_upgrades_finish_on_a_worker_before_jobs_start(self):
        event_thread = threading.get_ident()
        for cls, module, method, loops in (
            (Achievements, "achievements", "init_database", (
                "check_time_achievements", "initial_achievement_check", "cleanup_database",
                "salary_task", "raffle_hub_task",
            )),
        ):
            with self.subTest(feature=module):
                events = []

                def initialize():
                    self.assertNotEqual(threading.get_ident(), event_thread)
                    events.append("upgraded")

                owner = SimpleNamespace(**{loop: loop for loop in loops})
                setattr(owner, method, initialize)
                if cls is Achievements:
                    owner.init_achievements = lambda: events.append("definitions")
                with patch(f"elbow_helper.features.{module}.cog.start_resilient_loop",
                           side_effect=lambda loop: events.append(loop)):
                    await cls.cog_load(owner)
                self.assertEqual(events[0], "upgraded")
                self.assertTrue(set(loops) <= set(events))

    async def test_failed_upgrade_starts_no_background_jobs(self):
        def fail():
            raise UnversionedSQLiteSchemaError("synthetic unknown schema")
        with patch("elbow_helper.features.achievements.cog.start_resilient_loop") as start:
            with self.assertRaises(UnversionedSQLiteSchemaError):
                await Achievements.cog_load(SimpleNamespace(init_database=fail))
            start.assert_not_called()


    async def test_account_links_suggestion_state_failure_keeps_record_refresh_running(self):
        owner = SimpleNamespace(_poll_clans_loop=object(), register_pending_suggestion_views=AsyncMock(
            side_effect=sqlite3.OperationalError("synthetic suggestion failure")))
        with (patch("elbow_helper.features.account_links.cog.start_resilient_loop") as start,
              self.assertLogs("elbow_helper.features.account_links.cog", level="ERROR")):
            await AccountLinks.cog_load(owner)
        start.assert_called_once_with(owner._poll_clans_loop)


if __name__ == "__main__":
    unittest.main()
