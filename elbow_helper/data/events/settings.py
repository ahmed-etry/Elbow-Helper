"""Player health overrides and their original audit history."""

import sqlite3

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

