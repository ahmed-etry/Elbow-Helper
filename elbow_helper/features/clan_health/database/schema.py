"""Clan health's own small file: each clan's expectation settings and their change log.

Facts live in the family record. The settings and change log came from the old
clan health file once, at startup, with a receipt; after that the old file is
never opened by clan health. It stays on disk, untouched, for a later update to
delete.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
from pathlib import Path
import sqlite3

from elbow_helper.infrastructure.persistence.migrations import SQLiteMigration
from elbow_helper.infrastructure.persistence.versioned import VersionedSQLiteDatabase

LOGGER = logging.getLogger(__name__)

SETTINGS_COLUMNS = ("clan_code", "seed_template", "seeded_at_utc", "updated_at_utc", "payload_json")
AUDIT_COLUMNS = ("id", "ts_utc", "clan_code", "layer", "block", "key", "old_value", "new_value",
                 "actor_discord_id", "actor_display")


def create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clan_player_health_config (
            clan_code TEXT PRIMARY KEY,
            seed_template TEXT NOT NULL,
            seeded_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clan_health_config_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            clan_code TEXT NOT NULL,
            layer TEXT NOT NULL,
            block TEXT NOT NULL,
            key TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            actor_discord_id INTEGER NOT NULL,
            actor_display TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chca_clan_ts ON clan_health_config_audit(clan_code, ts_utc DESC)")
    # One row: the once-only copy from the old clan health file.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS old_file_copy (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            copied_at_utc TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN ('copied', 'no_old_file')),
            settings_rows INTEGER NOT NULL,
            audit_rows INTEGER NOT NULL,
            source_digest TEXT
        )
        """
    )


SCHEMA_VERSION = 1

# A new file only: an unversioned file with tables is refused, never adopted.
DATABASE = VersionedSQLiteDatabase(
    "clan health settings", create_schema, None,
    migrations=(SQLiteMigration(1, "clan health settings and change log", create_schema),),
    target_version=SCHEMA_VERSION,
)


def _old_rows(old_path: Path) -> tuple[list[tuple], list[tuple]]:
    """Both tables of the old file, read-only; a table the old file never had has no rows."""
    with closing(sqlite3.connect(old_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)) as source:
        source.execute("PRAGMA query_only=ON")
        source.execute("BEGIN")
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        settings = [tuple(row) for row in source.execute(
            f"SELECT {','.join(SETTINGS_COLUMNS)} FROM clan_player_health_config ORDER BY clan_code")] if (
            "clan_player_health_config" in tables) else []
        audit = [tuple(row) for row in source.execute(
            f"SELECT {','.join(AUDIT_COLUMNS)} FROM clan_health_config_audit ORDER BY id")] if (
            "clan_health_config_audit" in tables) else []
    return settings, audit


def copy_old_settings(path: Path, old_path: Path) -> dict | None:
    """Copy the settings and change log from the old file once; None when an earlier start already did.

    All or nothing: rows and receipt are written in one transaction, and the
    copied rows are read back and counted before it commits.
    """
    DATABASE.initialize(path)
    with closing(DATABASE.connect(path, timeout=30)) as conn:
        if conn.execute("SELECT 1 FROM old_file_copy").fetchone():
            return None
        if Path(old_path).is_file():
            settings, audit = _old_rows(Path(old_path))
            outcome = "copied"
            digest = sha256(json.dumps([settings, audit], default=str).encode()).hexdigest()
        else:
            settings, audit, outcome, digest = [], [], "no_old_file", None
        with conn:
            conn.executemany(f"INSERT INTO clan_player_health_config ({','.join(SETTINGS_COLUMNS)}) "
                             f"VALUES ({','.join('?' for _ in SETTINGS_COLUMNS)})", settings)
            conn.executemany(f"INSERT INTO clan_health_config_audit ({','.join(AUDIT_COLUMNS)}) "
                             f"VALUES ({','.join('?' for _ in AUDIT_COLUMNS)})", audit)
            copied = (conn.execute("SELECT COUNT(*) FROM clan_player_health_config").fetchone()[0],
                      conn.execute("SELECT COUNT(*) FROM clan_health_config_audit").fetchone()[0])
            if copied != (len(settings), len(audit)):
                raise sqlite3.IntegrityError("clan_health_settings_copy_mismatch")
            conn.execute("INSERT INTO old_file_copy VALUES (1,?,?,?,?,?)", (
                datetime.now(timezone.utc).replace(microsecond=0).isoformat(), outcome, len(settings), len(audit),
                digest))
    LOGGER.info("Clan health settings copied from the old file: outcome=%s settings=%s changes=%s",
                outcome, len(settings), len(audit))
    return {"outcome": outcome, "settings": len(settings), "audit": len(audit), "source_digest": digest}
