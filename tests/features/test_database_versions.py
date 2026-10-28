"""Adopt deployed schemas without losing data or accepting unknown formats."""

from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.account_links import schema as links_schema
from elbow_helper.features.account_links.database import AccountLinksDbMixin
from elbow_helper.features.account_links.cog import AccountLinks
from elbow_helper.features.achievements import schema as achievements_schema
from elbow_helper.features.achievements.database import AchievementsDatabaseMixin
from elbow_helper.features.achievements.cog import Achievements
from elbow_helper.features.clan_health.cog import ClanHealth
from elbow_helper.features.clan_health.database import ClanHealthRepository
from elbow_helper.features.clan_health.database import schema as health_schema
from elbow_helper.features.ranked_stats import schema as ranked_schema
from elbow_helper.features.ranked_stats.store import RankedStore
from elbow_helper.features.records.database import RecordRepository
from elbow_helper.features.records.database import schema as records_schema
from elbow_helper.features import records
from elbow_helper.infrastructure.persistence.migrations import (
    SQLiteMigrationError, UnversionedSQLiteSchemaError, UnsupportedSQLiteVersionError,
)
from elbow_helper.infrastructure.persistence.versioned import validate_sqlite_shape


SCHEMAS = (links_schema, ranked_schema, records_schema, achievements_schema, health_schema)


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


class DatabaseVersionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def path(self, schema, suffix=""):
        return self.root / f"{schema.DATABASE.name}{suffix}.sqlite3"

    def test_blank_databases_start_at_version_one(self):
        for schema in SCHEMAS:
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                schema.DATABASE.initialize(path)
                self.assertEqual(version(path), 1)
                with closing(sqlite3.connect(path)) as connection:
                    self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_current_unversioned_data_and_schema_are_unchanged_by_adoption(self):
        for schema in SCHEMAS:
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                deployed_fixture(path, schema)
                before = saved_state(path)
                schema.DATABASE.initialize(path)
                self.assertEqual(version(path), 1)
                self.assertEqual(saved_state(path), before)

    def assert_adopted_with_warnings(self, schema, path, messages):
        before = saved_state(path)
        with self.assertLogs("elbow_helper.infrastructure.persistence.versioned", level="WARNING") as logged:
            schema.DATABASE.initialize(path)
            schema.DATABASE.initialize(path)
        self.assertEqual(version(path), 1)
        self.assertEqual(saved_state(path), before)
        self.assertEqual(len(logged.output), len(messages), logged.output)
        for message in messages:
            self.assertTrue(any(message in line for line in logged.output), logged.output)

    def test_harmless_type_differences_adopt_without_rewriting_values(self):
        for schema, table, column in (
            (links_schema, "links", "player_name_last_seen"),
            (ranked_schema, "snapshots", "player_name"),
            (records_schema, "leadership_records", "member_display"),
            (achievements_schema, "achievements", "name"),
            (health_schema, "player_snapshots", "player_name"),
        ):
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                deployed_fixture(path, schema)
                rewrite_table(path, table, lambda sql: sql.replace(f"{column} TEXT", f"{column} VARCHAR"))
                self.assert_adopted_with_warnings(schema, path, [f"{table}.{column}: declared type differs"])

    def test_unused_defaults_adopt_without_rewriting_values(self):
        for schema, table, column, old in (
            (links_schema, "links", "last_seen_role", "TEXT DEFAULT ''"),
            (ranked_schema, "snapshots", "player_name", "TEXT"),
            (records_schema, "leadership_records", "note", "TEXT NOT NULL DEFAULT ''"),
            (achievements_schema, "user_coins", "balance", "INTEGER NOT NULL DEFAULT 0"),
            (health_schema, "player_snapshots", "pet_sum", "INTEGER DEFAULT 0"),
        ):
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                deployed_fixture(path, schema)
                # Writers supply these columns explicitly, so no default is needed.
                new = "TEXT DEFAULT 'older'" if old == "TEXT" else old.split(" DEFAULT")[0]
                rewrite_table(path, table, lambda sql: sql.replace(f"{column} {old}", f"{column} {new}"))
                self.assert_adopted_with_warnings(schema, path, [f"{table}.{column}: default differs"])

    def test_looser_nullability_adopts_without_rewriting_values(self):
        for schema, table, column, kind in (
            (links_schema, "links", "discord_user_id", "INTEGER"),
            (ranked_schema, "snapshots", "status", "TEXT"),
            (records_schema, "leadership_records", "note", "TEXT"),
            (achievements_schema, "achievements", "name", "TEXT"),
            (health_schema, "player_snapshots", "player_name", "TEXT"),
        ):
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                deployed_fixture(path, schema)
                rewrite_table(path, table, lambda sql: sql.replace(f"{column} {kind} NOT NULL", f"{column} {kind}"))
                self.assert_adopted_with_warnings(schema, path, [f"{table}.{column}: nullability differs"])

    def test_extra_columns_on_named_inserts_adopt_unchanged(self):
        for schema, table in (
            (links_schema, "links"), (ranked_schema, "snapshots"),
            (records_schema, "leadership_records"), (achievements_schema, "user_stats"),
            (health_schema, "player_snapshots"),
        ):
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                deployed_fixture(path, schema)
                with closing(sqlite3.connect(path)) as connection, connection:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN older_field BLOB")
                    connection.execute(f"UPDATE {table} SET older_field=x'00ff80'")
                self.assert_adopted_with_warnings(schema, path, [f"{table}.older_field: extra column"])

    def test_missing_foreign_keys_adopt_unchanged(self):
        for schema, table in (
            (achievements_schema, "user_achievements"), (health_schema, "report_players"),
        ):
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                deployed_fixture(path, schema)
                # DDL indentation differs between owners; use the exact stored clause.
                with closing(sqlite3.connect(path)) as connection:
                    sql = connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
                foreign_line = next(line for line in sql.splitlines() if "FOREIGN KEY" in line)
                rewrite_table(path, table, lambda sql: sql.replace(",\n" + foreign_line, ""))
                self.assert_adopted_with_warnings(schema, path, [f"{table}: missing foreign key"])

    def test_named_inserts_accept_reordered_columns(self):
        for schema, table in (
            (links_schema, "links"), (ranked_schema, "snapshots"),
            (records_schema, "leadership_records"), (achievements_schema, "achievements"),
            (health_schema, "player_snapshots"),
        ):
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                deployed_fixture(path, schema)

                def reorder(sql):
                    prefix, body = sql.split("(", 1)
                    first, second, rest = body.split(",", 2)
                    return prefix + "(" + second + "," + first + "," + rest

                rewrite_table(path, table, reorder)
                self.assert_adopted_with_warnings(schema, path, [f"{table}: column order differs"])

    def test_missing_identity_keys_are_refused_unchanged(self):
        for schema, table, old, new in (
            (links_schema, "links", "player_tag TEXT PRIMARY KEY", "player_tag TEXT"),
            (ranked_schema, "snapshots", ",\n        PRIMARY KEY (player_tag, observed_at)", ""),
            (records_schema, "leadership_records", "id INTEGER PRIMARY KEY AUTOINCREMENT", "id INTEGER"),
            (achievements_schema, "user_stats", "user_id INTEGER PRIMARY KEY", "user_id INTEGER"),
            (health_schema, "player_directory", "player_tag TEXT PRIMARY KEY", "player_tag TEXT"),
        ):
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                deployed_fixture(path, schema)
                rewrite_table(path, table, lambda sql: sql.replace(old, new))
                self.assert_refused_unchanged(schema, path, UnversionedSQLiteSchemaError)

    def test_achievement_defaults_used_by_omitted_inserts_stay_strict(self):
        for table, columns in achievements_schema._REQUIRED_DEFAULTS.items():
            for column in columns:
                with self.subTest(table=table, column=column):
                    path = self.path(achievements_schema, table + column)
                    deployed_fixture(path, achievements_schema)
                    with closing(sqlite3.connect(path)) as connection:
                        info = next(row for row in connection.execute(f"PRAGMA table_info({table})") if row[1] == column)
                    old = f"{column} {info[2]}" + (f" DEFAULT {info[4]}" if info[4] is not None else "")
                    rewrite_table(path, table, lambda sql: sql.replace(old, f"{column} {info[2]} DEFAULT 999"))
                    self.assert_refused_unchanged(achievements_schema, path, UnversionedSQLiteSchemaError)

    def test_required_nullability_stays_strict_for_explicit_missing_values(self):
        for schema, nullable in (
            (ranked_schema, ranked_schema._NULLABLE),
            (achievements_schema, {"coin_transactions": {"reason", "actor_id"}, "raffle_winners": {"reroll_of"}}),
            (health_schema, health_schema._NULLABLE),
        ):
            for table, columns in nullable.items():
                for column in columns:
                    if table == "ranked_battles" and column == "opponent_tag":
                        continue  # The existing upgrade makes this column nullable.
                    with self.subTest(database=schema.DATABASE.name, table=table, column=column):
                        path = self.path(schema, table + column)
                        deployed_fixture(path, schema)
                        with closing(sqlite3.connect(path)) as connection:
                            kind = next(row[2] for row in connection.execute(f"PRAGMA table_info({table})") if row[1] == column)
                        rewrite_table(path, table, lambda sql: sql.replace(f"{column} {kind}", f"{column} {kind} NOT NULL"))
                        self.assert_refused_unchanged(schema, path, UnversionedSQLiteSchemaError)

    def test_omitted_nullable_columns_with_usable_defaults_are_accepted(self):
        path = self.path(achievements_schema)
        deployed_fixture(path, achievements_schema)
        rewrite_table(path, "user_stats", lambda sql: sql.replace(
            "voice_never_unmuted INTEGER DEFAULT 1", "voice_never_unmuted INTEGER NOT NULL DEFAULT 1"))
        self.assert_adopted_with_warnings(achievements_schema, path, ["user_stats.voice_never_unmuted: nullability differs"])

    def test_null_defaults_are_accepted_when_the_reader_supplies_zero(self):
        path = self.path(achievements_schema)
        deployed_fixture(path, achievements_schema)
        rewrite_table(path, "user_stats", lambda sql: sql.replace("message_count INTEGER DEFAULT 0", "message_count INTEGER"))
        self.assert_adopted_with_warnings(achievements_schema, path, ["user_stats.message_count: default differs"])
        with closing(achievements_schema.DATABASE.connect(path)) as connection:
            connection.execute("INSERT INTO user_stats(user_id) VALUES (999)")
            count = connection.execute("SELECT message_count FROM user_stats WHERE user_id=999").fetchone()[0]
            self.assertEqual((count or 0) + 1, 1)

    def test_equivalent_numeric_default_expressions_are_accepted(self):
        path = self.path(achievements_schema)
        deployed_fixture(path, achievements_schema)
        rewrite_table(path, "user_stats", lambda sql: sql.replace("emoji_count INTEGER DEFAULT 0", "emoji_count INTEGER DEFAULT ('0')"))
        self.assert_adopted_with_warnings(achievements_schema, path, ["user_stats.emoji_count: default differs"])
        with closing(achievements_schema.DATABASE.connect(path)) as connection:
            connection.execute("INSERT INTO user_stats(user_id) VALUES (999)")
            connection.execute("UPDATE user_stats SET emoji_count=emoji_count+1 WHERE user_id=999")
            self.assertEqual(connection.execute("SELECT emoji_count FROM user_stats WHERE user_id=999").fetchone()[0], 1)

    def test_unusable_omitted_defaults_are_refused_unchanged(self):
        path = self.path(achievements_schema)
        deployed_fixture(path, achievements_schema)
        rewrite_table(path, "user_stats", lambda sql: sql.replace(
            "emoji_count INTEGER DEFAULT 0", "emoji_count INTEGER DEFAULT (missing_function())"))
        self.assert_refused_unchanged(achievements_schema, path, UnversionedSQLiteSchemaError)

    def test_health_accepts_missing_keys_unused_by_its_queries(self):
        for table, old, new, columns in (
            ("war_roster_members", ",\n            PRIMARY KEY (war_id, clan_code, player_tag)", "",
             ("war_id", "clan_code", "player_tag")),
            ("clan_health_config_audit", "id INTEGER PRIMARY KEY AUTOINCREMENT", "id INTEGER", ("id",)),
        ):
            with self.subTest(table=table):
                path = self.path(health_schema, table)
                deployed_fixture(path, health_schema)
                rewrite_table(path, table, lambda sql: sql.replace(old, new))
                self.assert_adopted_with_warnings(health_schema, path, [f"{table}.{column}: primary key declaration differs"
                                                                     for column in columns])

    def test_equivalent_unique_keys_accept_a_different_primary_key_declaration(self):
        for schema, table, column, kind in (
            (links_schema, "links", "player_tag", "TEXT"),
            (ranked_schema, "profile_health", "player_tag", "TEXT"),
            (achievements_schema, "user_stats", "user_id", "INTEGER"),
            (health_schema, "player_directory", "player_tag", "TEXT"),
        ):
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                deployed_fixture(path, schema)
                rewrite_table(path, table, lambda sql: sql.replace(f"{column} {kind} PRIMARY KEY", f"{column} {kind} UNIQUE"))
                self.assert_adopted_with_warnings(schema, path, [f"{table}.{column}: primary key declaration differs"])

    def test_positional_inserts_refuse_reordered_and_extra_columns(self):
        for table in ranked_schema._POSITIONAL_TABLES:
            for extra in (False, True):
                with self.subTest(table=table, extra=extra):
                    path = self.path(ranked_schema, table + str(extra))
                    deployed_fixture(path, ranked_schema)

                    def change(sql):
                        prefix, body = sql.split("(", 1)
                        if extra:
                            return prefix + "(older_field TEXT," + body
                        parts = body.rsplit(")", 1)[0].split(",", 2)
                        parts[0], parts[1] = parts[1], parts[0]
                        return prefix + "(" + ",".join(parts) + ")"

                    rewrite_table(path, table, change, extra_values={"older_field": "preserved"} if extra else None)
                    self.assert_refused_unchanged(ranked_schema, path, UnversionedSQLiteSchemaError)

    def test_generated_ids_refuse_type_or_default_changes(self):
        for schema, table in (
            (records_schema, "leadership_records"),
            (achievements_schema, "coin_transactions"), (achievements_schema, "raffle_winners"),
        ):
            for old, new in (
                ("id INTEGER PRIMARY KEY AUTOINCREMENT", "id BIGINT PRIMARY KEY"),
                ("id INTEGER PRIMARY KEY AUTOINCREMENT", "id INTEGER PRIMARY KEY DEFAULT 0"),
            ):
                with self.subTest(table=table, change=new):
                    path = self.path(schema, table + new)
                    deployed_fixture(path, schema)
                    rewrite_table(path, table, lambda sql: sql.replace(old, new))
                    self.assert_refused_unchanged(schema, path, UnversionedSQLiteSchemaError)

    def test_omitted_required_columns_without_defaults_are_refused(self):
        for schema, table in ((links_schema, "links"), (ranked_schema, "snapshots"),
                              (records_schema, "leadership_records"), (achievements_schema, "achievements"),
                              (health_schema, "player_snapshots")):
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                deployed_fixture(path, schema)
                rewrite_table(path, table, lambda sql: sql.replace("(", "(older_field TEXT NOT NULL,", 1),
                              extra_values={"older_field": "preserved"})
                self.assert_refused_unchanged(schema, path, UnversionedSQLiteSchemaError)

    def test_unrelated_unversioned_files_are_refused_unchanged(self):
        for schema in SCHEMAS:
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                with closing(sqlite3.connect(path)) as connection, connection:
                    connection.execute("CREATE TABLE unknown (value BLOB)")
                    connection.execute("INSERT INTO unknown VALUES (x'00ff')")
                self.assert_refused_unchanged(schema, path, UnversionedSQLiteSchemaError)

    def test_missing_required_columns_are_refused_unchanged(self):
        for schema, table, column in (
            (links_schema, "links", "last_seen_role"),
            (ranked_schema, "snapshots", "fields_json"),
            (records_schema, "leadership_records", "note"),
            (achievements_schema, "user_stats", "meme_posts"),
            (health_schema, "report_players", "note"),
        ):
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                deployed_fixture(path, schema)
                with closing(sqlite3.connect(path)) as connection, connection:
                    connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
                self.assert_refused_unchanged(schema, path, UnversionedSQLiteSchemaError)

    def test_missing_required_tables_are_refused_unchanged(self):
        for schema, table in (
            (links_schema, "suggestions"), (ranked_schema, "snapshots"),
            (records_schema, "leadership_records"), (achievements_schema, "user_stats"),
            (health_schema, "player_directory"),
        ):
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                deployed_fixture(path, schema)
                with closing(sqlite3.connect(path)) as connection, connection:
                    connection.execute(f"DROP TABLE {table}")
                self.assert_refused_unchanged(schema, path, UnversionedSQLiteSchemaError)

    def test_newer_versions_are_refused_before_any_writes(self):
        for schema in SCHEMAS:
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                deployed_fixture(path, schema)
                with closing(sqlite3.connect(path)) as connection:
                    connection.execute("PRAGMA user_version=2")
                self.assert_refused_unchanged(schema, path, UnsupportedSQLiteVersionError)

    def assert_refused_unchanged(self, schema, path, error):
        before = path.read_bytes()
        with self.assertLogs("elbow_helper.infrastructure.persistence.versioned", level="ERROR") as logged:
            with self.assertRaises(error):
                schema.DATABASE.initialize(path)
        self.assertIn(schema.DATABASE.name, logged.output[0])
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(path.with_name(path.name + "-wal").exists())

    def test_links_primary_column_upgrade_preserves_other_data(self):
        path = self.path(links_schema)
        deployed_fixture(path, links_schema)
        before = saved_state(path)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("ALTER TABLE links ADD COLUMN is_primary INTEGER NOT NULL DEFAULT 0")
        links_schema.DATABASE.initialize(path)
        self.assertEqual(version(path), 1)
        self.assertEqual(saved_state(path), before)

    def test_ranked_snapshot_only_and_nullable_opponent_upgrades(self):
        for snapshot_only in (True, False):
            with self.subTest(snapshot_only=snapshot_only):
                path = self.path(ranked_schema, str(snapshot_only))
                deployed_fixture(path, ranked_schema)
                with closing(sqlite3.connect(path)) as connection, connection:
                    snapshots = connection.execute("SELECT * FROM snapshots").fetchall()
                    battles = connection.execute("SELECT * FROM ranked_battles").fetchall()
                    if snapshot_only:
                        for table in ("ranked_battles", "battle_log_polls", "profile_health"):
                            connection.execute(f"DROP TABLE {table}")
                    else:
                        sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='ranked_battles'").fetchone()[0]
                        connection.execute("ALTER TABLE ranked_battles RENAME TO saved_battles")
                        connection.execute(sql.replace("opponent_tag TEXT", "opponent_tag TEXT NOT NULL"))
                        connection.execute("INSERT INTO ranked_battles SELECT * FROM saved_battles")
                        connection.execute("DROP TABLE saved_battles")
                ranked_schema.DATABASE.initialize(path)
                self.assertEqual(version(path), 1)
                with closing(sqlite3.connect(path)) as connection:
                    self.assertEqual(connection.execute("SELECT * FROM snapshots").fetchall(), snapshots)
                    self.assertEqual(connection.execute("SELECT * FROM ranked_battles").fetchall(), [] if snapshot_only else battles)
                    self.assertEqual(next(row[3] for row in connection.execute("PRAGMA table_info(ranked_battles)")
                                          if row[1] == "opponent_tag"), 0)

    def test_achievement_reward_ledger_is_backfilled_once(self):
        path = self.path(achievements_schema)
        deployed_fixture(path, achievements_schema)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("DROP TABLE cwl_reward_grants")
            connection.execute("UPDATE coin_transactions SET type='bonus_cwl', reason='synthetic_bonus'")
            transactions = connection.execute("SELECT * FROM coin_transactions").fetchall()
        achievements_schema.DATABASE.initialize(path)
        achievements_schema.DATABASE.initialize(path)
        self.assertEqual(version(path), 1)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("SELECT * FROM coin_transactions").fetchall(), transactions)
            self.assertEqual(connection.execute("SELECT * FROM cwl_reward_grants").fetchall(),
                             [("synthetic_bonus", 7, "coins", 7)])

    def test_health_label_upgrade_preserves_the_original_label(self):
        path = self.path(health_schema)
        deployed_fixture(path, health_schema)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("ALTER TABLE wars DROP COLUMN cwl_season_label")
            connection.execute("UPDATE wars SET war_type='CWL', cwl_season='2026-09', start_ts=1788220800, cwl_round=1")
            other_data = connection.execute("SELECT * FROM report_players").fetchall()
        health_schema.DATABASE.initialize(path)
        self.assertEqual(version(path), 1)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("SELECT cwl_season, cwl_season_label FROM wars").fetchall(),
                             [("2026-09", "2026-09")])
            self.assertEqual(connection.execute("SELECT * FROM report_players").fetchall(), other_data)

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
                self.assertEqual(version(path), 1)

    def test_concurrent_first_connections_adopt_only_once(self):
        for schema in SCHEMAS:
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                deployed_fixture(path, schema)
                with patch.object(schema.DATABASE, "adopt", wraps=schema.DATABASE.adopt) as adopt:
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        list(pool.map(schema.DATABASE.initialize, [path] * 8))
                    adopt.assert_called_once()
                self.assertEqual(version(path), 1)

    def test_connections_never_recreate_a_file_removed_after_startup(self):
        for schema in SCHEMAS:
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                schema.DATABASE.initialize(path)
                path.unlink()
                with self.assertRaises(sqlite3.OperationalError):
                    schema.DATABASE.connect(path)
                self.assertFalse(path.exists())

    def test_ordinary_connections_cannot_skip_adoption(self):
        for schema in SCHEMAS:
            with self.subTest(database=schema.DATABASE.name):
                path = self.path(schema)
                deployed_fixture(path, schema)
                before = saved_state(path)
                self.open_through_feature(schema, path)
                self.assertEqual(version(path), 1)
                self.assertEqual(saved_state(path), before)

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
        if schema is links_schema:
            with patch("elbow_helper.features.account_links.database.DB_PATH", path):
                AccountLinksDbMixin().get_all_links()
        elif schema is ranked_schema:
            RankedStore(path).append([])
        elif schema is records_schema:
            RecordRepository(path).list()
        elif schema is health_schema:
            ClanHealthRepository(path).search_players("")
        else:
            owner = AchievementsDatabaseMixin()
            owner.db_path = path
            owner.logger = logging.getLogger(__name__)
            owner.ALL_ACHIEVEMENTS = ()
            owner.init_achievements()


class SchemaShapeTests(unittest.TestCase):
    def create(self, connection):
        connection.execute("CREATE TABLE parents (id INTEGER PRIMARY KEY)")
        connection.execute("""CREATE TABLE facts (
            id INTEGER PRIMARY KEY, value TEXT NOT NULL DEFAULT '', parent INTEGER,
            UNIQUE(value), FOREIGN KEY(parent) REFERENCES parents(id)
        )""")

    def test_missing_identity_constraints_are_rejected(self):
        for old, new in (
            ("id INTEGER PRIMARY KEY,", "id INTEGER,"),
            ("UNIQUE(value),", ""),
        ):
            with self.subTest(change=new), closing(sqlite3.connect(":memory:")) as connection:
                self.create(connection)
                sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='facts'").fetchone()[0]
                connection.execute("DROP TABLE facts")
                connection.execute(sql.replace(old, new))
                with self.assertRaises(UnversionedSQLiteSchemaError):
                    validate_sqlite_shape(connection, self.create)

    def test_other_schema_differences_only_warn_without_query_dependencies(self):
        for old, new, warning in (
            ("value TEXT", "value VARCHAR", "declared type"),
            ("TEXT NOT NULL", "TEXT", "nullability"),
            ("DEFAULT ''", "DEFAULT 'different'", "default"),
            (", FOREIGN KEY(parent) REFERENCES parents(id)", "", "missing foreign key"),
        ):
            with self.subTest(change=new), closing(sqlite3.connect(":memory:")) as connection:
                self.create(connection)
                sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='facts'").fetchone()[0]
                connection.execute("DROP TABLE facts")
                connection.execute(sql.replace(old, new))
                with self.assertLogs("elbow_helper.infrastructure.persistence.versioned", level="WARNING") as logged:
                    validate_sqlite_shape(connection, self.create)
                self.assertEqual(len(logged.output), 1)
                self.assertIn(warning, logged.output[0])

    def test_positional_inserts_require_the_deployed_column_order(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            self.create(connection)
            sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='facts'").fetchone()[0]
            connection.execute("DROP TABLE facts")
            connection.execute(sql.replace("id INTEGER PRIMARY KEY, value TEXT NOT NULL DEFAULT '', parent INTEGER",
                                           "value TEXT NOT NULL DEFAULT '', id INTEGER PRIMARY KEY, parent INTEGER"))
            with self.assertRaisesRegex(UnversionedSQLiteSchemaError, "column order"):
                validate_sqlite_shape(connection, self.create, positional_tables=frozenset({"facts"}))


class DatabaseStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_upgrade_runs_on_a_worker_before_registration(self):
        event_thread = threading.get_ident()
        events = []

        def initialize():
            self.assertNotEqual(threading.get_ident(), event_thread)
            events.append("upgraded")

        bot = SimpleNamespace(
            get_cog=lambda name: object(), add_cog=AsyncMock(),
            workbook_writer=object(), google_publisher=object(), local_exports=object(),
        )
        with patch.object(records, "RecordRepository", return_value=SimpleNamespace(initialize=initialize)), patch.object(
            records, "RecordService"
        ), patch.object(records, "RecordExportService"), patch.object(records, "RecordWorkbookWriter"), patch.object(
            records, "Records"
        ):
            await records.setup(bot)
        self.assertEqual(events, ["upgraded"])
        bot.add_cog.assert_awaited_once()

    async def test_records_refuses_registration_after_an_upgrade_failure(self):
        def fail():
            raise UnversionedSQLiteSchemaError("synthetic unknown schema")

        bot = SimpleNamespace(get_cog=lambda name: object(), add_cog=AsyncMock())
        with patch.object(records, "RecordRepository", return_value=SimpleNamespace(initialize=fail)):
            with self.assertRaises(UnversionedSQLiteSchemaError):
                await records.setup(bot)
        bot.add_cog.assert_not_called()

    async def test_startup_upgrades_finish_on_a_worker_before_jobs_start(self):
        event_thread = threading.get_ident()
        for cls, module, method, loops in (
            (AccountLinks, "account_links", "_init_db", ("_poll_clans_loop",)),
            (Achievements, "achievements", "init_database", (
                "check_time_achievements", "initial_achievement_check", "cleanup_database",
                "salary_task", "raffle_hub_task",
            )),
            (ClanHealth, "clan_health", "initialize", ("_war_log_loop", "_snapshot_log_loop")),
        ):
            with self.subTest(feature=module):
                events = []

                def initialize():
                    self.assertNotEqual(threading.get_ident(), event_thread)
                    events.append("upgraded")

                owner = SimpleNamespace(**{loop: loop for loop in loops})
                if cls is ClanHealth:
                    owner.repository = SimpleNamespace(initialize=initialize)
                else:
                    setattr(owner, method, initialize)
                if cls is AccountLinks:
                    async def views():
                        events.append("views")
                    owner.register_pending_suggestion_views = views
                if cls is Achievements:
                    owner.init_achievements = lambda: events.append("definitions")
                with patch(f"elbow_helper.features.{module}.cog.start_resilient_loop",
                           side_effect=lambda loop: events.append(loop)), patch(
                    "elbow_helper.features.clan_health.database.config_store.seed_missing_configs"
                ):
                    await cls.cog_load(owner)
                self.assertEqual(events[0], "upgraded")
                self.assertTrue(set(loops) <= set(events))

    async def test_failed_upgrade_starts_no_background_jobs(self):
        for cls, module, method in (
            (AccountLinks, "account_links", "_init_db"),
            (Achievements, "achievements", "init_database"),
            (ClanHealth, "clan_health", "initialize"),
        ):
            with self.subTest(feature=module):
                def fail():
                    raise UnversionedSQLiteSchemaError("synthetic unknown schema")
                owner = SimpleNamespace()
                if cls is ClanHealth:
                    owner.repository = SimpleNamespace(initialize=fail)
                else:
                    setattr(owner, method, fail)
                with patch(f"elbow_helper.features.{module}.cog.start_resilient_loop") as start:
                    with self.assertRaises(UnversionedSQLiteSchemaError):
                        await cls.cog_load(owner)
                    start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
