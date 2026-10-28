"""Version-one schema and connection boundary."""

from __future__ import annotations

import sqlite3

from elbow_helper.infrastructure.persistence.migrations import SQLiteMigration
from elbow_helper.infrastructure.persistence.versioned import (
    VersionedSQLiteDatabase, validate_sqlite_shape,
)


def create_schema(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS leadership_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_ts INTEGER NOT NULL,
            updated_ts INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            member_id INTEGER NOT NULL,
            member_display TEXT NOT NULL,
            category_key TEXT NOT NULL,
            incident_type_key TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            recorder_id INTEGER NOT NULL,
            recorder_display TEXT NOT NULL,
            edited_by_id INTEGER,
            edited_by_display TEXT,
            removed_ts INTEGER,
            removed_by_id INTEGER,
            removed_by_display TEXT
        )
        """
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_leadership_records_member_status "
        "ON leadership_records(member_id, status, created_ts DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_leadership_records_created "
        "ON leadership_records(created_ts DESC)"
    )


def validate_schema(conn: sqlite3.Connection, *, warn: bool = True) -> None:
    # _insert_record omits id then selects by cursor.lastrowid. Other omitted
    # columns are optional edit/removal metadata; all reads use column names.
    validate_sqlite_shape(
        conn, create_schema,
        generated_ids={"leadership_records": "id"},
        omitted_columns={"leadership_records": {
            "id", "edited_by_id", "edited_by_display", "removed_ts",
            "removed_by_id", "removed_by_display",
        }},
        warn=warn,
    )


def adopt_schema(conn: sqlite3.Connection) -> None:
    validate_schema(conn, warn=False)


SCHEMA_VERSION = 1

DATABASE = VersionedSQLiteDatabase(
    "leadership records", create_schema, adopt_schema,
    migrations=(SQLiteMigration(1, "leadership records baseline", create_schema),),
    target_version=SCHEMA_VERSION, validate=validate_schema,
)


class RecordSchema:
    def _init_db(self) -> None:
        DATABASE.initialize(self.path)

    def _db_connect(self, **kwargs) -> sqlite3.Connection:
        return DATABASE.connect(self.path, **kwargs)
