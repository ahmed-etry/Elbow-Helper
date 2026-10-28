"""Version-one account-link schema and supported primary-column upgrade."""

import sqlite3

from elbow_helper.infrastructure.persistence.migrations import SQLiteMigration
from elbow_helper.infrastructure.persistence.versioned import (
    VersionedSQLiteDatabase, validate_sqlite_shape,
)


def create_schema(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(links)")}
    if "is_primary" in columns:
        cursor.execute("ALTER TABLE links DROP COLUMN is_primary")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS links (
            player_tag TEXT PRIMARY KEY,
            discord_user_id INTEGER NOT NULL,
            player_name_last_seen TEXT DEFAULT '',
            last_seen_clan_tag TEXT DEFAULT '',
            last_seen_clan_code TEXT DEFAULT '',
            last_seen_role TEXT DEFAULT ''
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS suggestions (
            player_tag TEXT PRIMARY KEY,
            player_name TEXT NOT NULL,
            current_clan_code TEXT NOT NULL,
            current_clan_tag TEXT NOT NULL,
            proposed_discord_user_id INTEGER NOT NULL,
            proposed_display_name TEXT DEFAULT '',
            review_channel_id INTEGER DEFAULT 0,
            review_message_id INTEGER DEFAULT 0
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ignored_tags (
            player_tag TEXT PRIMARY KEY
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_links_discord_user ON links(discord_user_id)")


def validate_schema(conn: sqlite3.Connection, *, warn: bool = True) -> None:
    # All inserts name their columns; SELECT * is read by name. Only the three
    # player_tag keys are needed for ON CONFLICT / INSERT OR IGNORE.
    validate_sqlite_shape(conn, create_schema, warn=warn)


def adopt_schema(conn: sqlite3.Connection) -> None:
    legacy = any(row[1] == "is_primary" for row in conn.execute("PRAGMA table_info(links)"))
    validate_sqlite_shape(
        conn, create_schema,
        optional_tables=frozenset({"suggestions", "ignored_tags"}) if legacy else frozenset(),
        warn=False,
    )
    create_schema(conn)
    validate_schema(conn, warn=False)


SCHEMA_VERSION = 1

DATABASE = VersionedSQLiteDatabase(
    "account links", create_schema, adopt_schema,
    migrations=(SQLiteMigration(1, "account links baseline", create_schema),),
    target_version=SCHEMA_VERSION, validate=validate_schema,
)
