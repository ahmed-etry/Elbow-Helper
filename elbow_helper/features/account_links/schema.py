"""Account-link suggestion and ignore state; ownership lives in the family record."""

import sqlite3

from elbow_helper.infrastructure.persistence.migrations import SQLiteMigration
from elbow_helper.infrastructure.persistence.versioned import (
    VersionedSQLiteDatabase, validate_sqlite_shape,
)


def create_schema(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
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


def validate_schema(conn: sqlite3.Connection, *, warn: bool = True) -> None:
    # Inserts and reads name their columns; player_tag keys support upserts.
    validate_sqlite_shape(conn, create_schema, warn=warn)


SCHEMA_VERSION = 1

DATABASE = VersionedSQLiteDatabase(
    "account links", create_schema, None,
    migrations=(SQLiteMigration(1, "account links baseline", create_schema),),
    target_version=SCHEMA_VERSION, validate=validate_schema,
)
