"""Schema ownership and validation for the Rosters SQLite database."""

from __future__ import annotations

import sqlite3

from elbow_helper.infrastructure.persistence.migrations import SQLiteMigration
from elbow_helper.infrastructure.persistence.migrations import run_sqlite_migrations

from ..config import ROSTER_DISCORD_COLUMN_MAX_WIDTH
from ..config import ROSTER_DISCORD_COLUMN_MIN_WIDTH
from ..config import ROSTER_DISCORD_COLUMN_WIDTH
from ..config import ROSTER_PLAYER_COLUMN_MAX_WIDTH
from ..config import ROSTER_PLAYER_COLUMN_MIN_WIDTH
from ..config import ROSTER_PLAYER_COLUMN_WIDTH


SCHEMA_VERSION = 4

_EXPECTED_COLUMNS = {
    "rosters": {
        "id",
        "guild_id",
        "name",
        "clan_code",
        "role_id",
        "max_members",
        "min_townhall",
        "buttons_hidden",
        "status",
        "schedule_enabled",
        "schedule_utc_offset",
        "open_day",
        "open_time",
        "close_day",
        "close_time",
        "one_off_open_ts",
        "one_off_close_ts",
        "reset_on_open",
        "active_cycle_id",
        "last_open_cycle_key",
        "last_close_cycle_key",
        "google_sheet_id",
        "created_ts",
        "updated_ts",
    },
    "roster_cycles": {
        "id",
        "roster_id",
        "cycle_key",
        "opened_ts",
        "closed_ts",
    },
    "roster_members": {
        "roster_id",
        "cycle_id",
        "player_tag",
        "discord_user_id",
        "player_name",
        "clan_code",
        "townhall",
        "hero_sum",
        "signed_up_ts",
    },
    "roster_automation_events": {
        "roster_id",
        "cycle_key",
        "event_key",
        "claimed_ts",
    },
    "roster_posts": {
        "message_id",
        "roster_id",
        "channel_id",
        "created_ts",
    },
    "roster_layouts": {
        "roster_id",
        "show_townhall",
        "show_discord",
        "show_clan",
        "player_width",
        "discord_width",
    },
}
_EXPECTED_INDEXES = {
    "idx_roster_members_discord",
    "idx_rosters_schedule",
    "idx_roster_posts_roster",
}


class RosterSchemaError(RuntimeError):
    """Raised when a versioned Rosters database is structurally incomplete."""


def initialize_roster_schema(connection: sqlite3.Connection) -> None:
    """Create a blank v4 database or validate an existing v4 database."""
    connection.execute("PRAGMA journal_mode=WAL")
    run_sqlite_migrations(
        connection,
        _MIGRATIONS,
        target_version=SCHEMA_VERSION,
    )
    validate_roster_schema(connection)


def validate_roster_schema(connection: sqlite3.Connection) -> None:
    """Require every table, column, and index used by the repository."""
    for table, expected_columns in _EXPECTED_COLUMNS.items():
        rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        actual_columns = {str(row[1]) for row in rows}
        missing_columns = expected_columns - actual_columns
        if not rows:
            raise RosterSchemaError(f"Roster database is missing table {table}.")
        if missing_columns:
            missing = ", ".join(sorted(missing_columns))
            raise RosterSchemaError(
                f"Roster database table {table} is missing columns: {missing}."
            )

    actual_indexes = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    missing_indexes = _EXPECTED_INDEXES - actual_indexes
    if missing_indexes:
        missing = ", ".join(sorted(missing_indexes))
        raise RosterSchemaError(
            f"Roster database is missing indexes: {missing}."
        )


def _create_supported_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE rosters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL COLLATE NOCASE,
            clan_code TEXT NOT NULL DEFAULT 'FAMILY',
            role_id INTEGER,
            max_members INTEGER NOT NULL DEFAULT 500,
            min_townhall INTEGER,
            buttons_hidden INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'closed',
            schedule_enabled INTEGER NOT NULL DEFAULT 0,
            schedule_utc_offset TEXT,
            open_day TEXT,
            open_time TEXT,
            close_day TEXT,
            close_time TEXT,
            one_off_open_ts INTEGER,
            one_off_close_ts INTEGER,
            reset_on_open INTEGER NOT NULL DEFAULT 1,
            active_cycle_id INTEGER,
            last_open_cycle_key TEXT,
            last_close_cycle_key TEXT,
            google_sheet_id TEXT,
            created_ts INTEGER NOT NULL,
            updated_ts INTEGER NOT NULL,
            UNIQUE(guild_id, name)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE roster_cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            roster_id INTEGER NOT NULL REFERENCES rosters(id) ON DELETE CASCADE,
            cycle_key TEXT NOT NULL,
            opened_ts INTEGER NOT NULL,
            closed_ts INTEGER,
            UNIQUE(roster_id, cycle_key)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE roster_members (
            roster_id INTEGER NOT NULL REFERENCES rosters(id) ON DELETE CASCADE,
            cycle_id INTEGER NOT NULL REFERENCES roster_cycles(id) ON DELETE CASCADE,
            player_tag TEXT NOT NULL,
            discord_user_id INTEGER NOT NULL,
            player_name TEXT NOT NULL,
            clan_code TEXT NOT NULL DEFAULT '',
            townhall INTEGER NOT NULL DEFAULT 0,
            hero_sum INTEGER NOT NULL DEFAULT 0,
            signed_up_ts INTEGER NOT NULL,
            PRIMARY KEY(roster_id, cycle_id, player_tag)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_roster_members_discord
        ON roster_members(roster_id, cycle_id, discord_user_id)
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_rosters_schedule
        ON rosters(schedule_enabled, status)
        """
    )
    connection.execute(
        """
        CREATE TABLE roster_automation_events (
            roster_id INTEGER NOT NULL REFERENCES rosters(id) ON DELETE CASCADE,
            cycle_key TEXT NOT NULL,
            event_key TEXT NOT NULL,
            claimed_ts INTEGER NOT NULL,
            PRIMARY KEY(roster_id, cycle_key, event_key)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE roster_posts (
            message_id INTEGER PRIMARY KEY,
            roster_id INTEGER NOT NULL REFERENCES rosters(id) ON DELETE CASCADE,
            channel_id INTEGER NOT NULL,
            created_ts INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_roster_posts_roster
        ON roster_posts(roster_id)
        """
    )
    connection.execute(
        f"""
        CREATE TABLE roster_layouts (
            roster_id INTEGER PRIMARY KEY REFERENCES rosters(id) ON DELETE CASCADE,
            show_townhall INTEGER NOT NULL DEFAULT 1,
            show_discord INTEGER NOT NULL DEFAULT 1,
            show_clan INTEGER NOT NULL DEFAULT 1,
            player_width INTEGER NOT NULL DEFAULT {ROSTER_PLAYER_COLUMN_WIDTH}
                CHECK(player_width BETWEEN {ROSTER_PLAYER_COLUMN_MIN_WIDTH}
                    AND {ROSTER_PLAYER_COLUMN_MAX_WIDTH}),
            discord_width INTEGER NOT NULL DEFAULT {ROSTER_DISCORD_COLUMN_WIDTH}
                CHECK(discord_width BETWEEN {ROSTER_DISCORD_COLUMN_MIN_WIDTH}
                    AND {ROSTER_DISCORD_COLUMN_MAX_WIDTH})
        )
        """
    )


_MIGRATIONS = (
    SQLiteMigration(
        version=SCHEMA_VERSION,
        name="supported Rosters schema baseline",
        apply=_create_supported_schema,
    ),
)
