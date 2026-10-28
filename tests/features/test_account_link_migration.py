"""Account-primary removal preserves link data and SQLite schema invariants."""

import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tests.data.link_fixtures import attach_links
from elbow_helper.features.account_links.database import AccountLinksDbMixin
from elbow_helper.features.account_links.schema import DATABASE, create_schema
from elbow_helper.infrastructure.persistence.migrations import SQLiteMigrationError


class AccountLinkMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "links.db"
        self.patcher = patch("elbow_helper.features.account_links.database.DB_PATH", self.path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.links = AccountLinksDbMixin()

    def legacy_database(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            create_schema(conn)
            conn.execute("ALTER TABLE links ADD COLUMN is_primary INTEGER NOT NULL DEFAULT 0")
            conn.execute("DROP TABLE suggestions")
            conn.execute("DROP TABLE ignored_tags")
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE UNIQUE INDEX unique_observation ON links(player_name_last_seen, player_tag);
                CREATE TABLE unrelated (value TEXT CHECK(value != ''));
                INSERT INTO unrelated VALUES ('untouched');
                CREATE TRIGGER reject_negative_member BEFORE INSERT ON links
                WHEN NEW.discord_user_id < 0 BEGIN SELECT RAISE(ABORT, 'invalid member'); END;
            """)
            conn.executemany("INSERT INTO links(player_tag, discord_user_id, is_primary, "
                             "player_name_last_seen, last_seen_clan_tag, last_seen_clan_code, last_seen_role) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?)", [
                ("#P0", 7, 1, "Éléphant 🐘", "#CLAN", "BEH", "coLeader"),
                ("#P2", 7, 0, "", "", "", ""),
                ("#P8", 8, 1, "玩家", None, "", None),
                ("#P9", 8, 0, None, "#OTHER", None, "member"),
            ])

    def preserved_state(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            return (
                conn.execute("SELECT player_tag, discord_user_id, player_name_last_seen, "
                             "last_seen_clan_tag, last_seen_clan_code, last_seen_role "
                             "FROM links ORDER BY player_tag").fetchall(),
                conn.execute("SELECT type, name, sql FROM sqlite_schema WHERE "
                             "type IN ('index', 'trigger') AND tbl_name IN ('links', 'unrelated') ORDER BY name").fetchall(),
                conn.execute("SELECT * FROM unrelated").fetchall(),
                conn.execute("SELECT sql FROM sqlite_schema WHERE name = 'unrelated'").fetchall(),
                conn.execute("PRAGMA journal_mode").fetchone(),
                conn.execute("PRAGMA user_version").fetchone(),
                conn.execute("PRAGMA application_id").fetchone(),
            )

    def test_migrate_twice_preserves_all_other_values_indexes_and_constraints(self):
        self.legacy_database()
        before = self.preserved_state()
        stamped = (*before[:5], (1,), *before[6:])
        for _ in range(2):
            self.links._init_db()
            self.assertEqual(self.preserved_state(), stamped)
            with self.links._db_connect() as conn:
                self.assertNotIn("is_primary", [row["name"] for row in conn.execute("PRAGMA table_info(links)")])
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                for sql in (
                    "INSERT INTO links(player_tag, discord_user_id) VALUES ('#P0', 99)",
                    "INSERT INTO links(player_tag, discord_user_id) VALUES ('#NULL', NULL)",
                    "INSERT INTO links(player_tag, discord_user_id) VALUES ('#NEG', -1)",
                ):
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(sql)
        asyncio.run(attach_links(self.links))
        self.assertEqual(len(self.links.get_all_links()), 4)
        self.assertEqual([row["player_tag"] for row in self.links.get_links_for_members([7, 8])[7]], ["#P0", "#P2"])

    def test_dependency_failure_leaves_original_database_valid(self):
        self.legacy_database()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("CREATE INDEX unexpected_primary_dependency ON links(is_primary)")
        before = self.preserved_state()
        with self.assertRaises(SQLiteMigrationError) as raised:
            self.links._init_db()
        self.assertIsInstance(raised.exception.__cause__, sqlite3.OperationalError)
        self.assertEqual(self.preserved_state(), before)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            self.assertEqual(conn.execute("SELECT is_primary FROM links ORDER BY player_tag").fetchall(), [(1,), (0,), (1,), (0,)])
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_failure_after_drop_rolls_back_schema_and_data(self):
        self.legacy_database()
        before = self.preserved_state()
        original_adopt = DATABASE.adopt

        def deny_later_schema_change(conn):
            conn.set_authorizer(lambda action, name, *args: (
                sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_TABLE
                and name == "suggestions" else sqlite3.SQLITE_OK
            ))
            original_adopt(conn)

        with patch.object(DATABASE, "adopt", deny_later_schema_change):
            with self.assertRaises(SQLiteMigrationError) as raised:
                self.links._init_db()
            self.assertIsInstance(raised.exception.__cause__, sqlite3.DatabaseError)
        self.assertEqual(self.preserved_state(), before)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            self.assertEqual(conn.execute("SELECT sum(is_primary) FROM links").fetchone()[0], 2)
        self.links._init_db()
        self.assertEqual(self.preserved_state(), (*before[:5], (1,), *before[6:]))

    def test_fresh_database_supports_link_reassignment_removal_and_restore(self):
        self.links._init_db()
        self.links._init_db()
        asyncio.run(attach_links(self.links))
        asyncio.run(self.links.upsert_links([
            {"player_tag": "#P0", "discord_user_id": 7, "player_name_last_seen": "Alpha"},
            {"player_tag": "#P2", "discord_user_id": 7, "player_name_last_seen": "Beta"},
        ]))
        before = self.links.get_links_by_tags(["#P0", "#P2", "#P8"])
        asyncio.run(self.links.upsert_link(player_tag="#P0", discord_user_id=8))
        asyncio.run(self.links.delete_link("#P2"))
        asyncio.run(self.links.upsert_link(player_tag="#P8", discord_user_id=8))
        asyncio.run(self.links.restore_links(before))
        self.assertEqual(self.links.get_links_by_tags(before), before)
        self.assertTrue(all("is_primary" not in row for row in self.links.get_all_links().values()))
        with self.links._db_connect() as conn:
            self.assertNotIn("is_primary", [row["name"] for row in conn.execute("PRAGMA table_info(links)")])
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
