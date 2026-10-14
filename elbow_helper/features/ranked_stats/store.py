"""Persistent player observations with historical ownership and clan membership."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3


class RankedStore:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=30)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS snapshots (
                player_tag TEXT NOT NULL, observed_at TEXT NOT NULL,
                member_id INTEGER NOT NULL, clan_tag TEXT,
                player_name TEXT, status TEXT NOT NULL, fields_json TEXT NOT NULL,
                PRIMARY KEY (player_tag, observed_at)
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS snapshots_member ON snapshots(member_id, observed_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS snapshots_clan ON snapshots(clan_tag, observed_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS snapshots_time ON snapshots(observed_at)")
            conn.commit()

    def append(self, observations):
        with closing(sqlite3.connect(self.path, timeout=30)) as conn:
            conn.executemany("""INSERT OR IGNORE INTO snapshots
                (player_tag, observed_at, member_id, clan_tag, player_name, status, fields_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)""", [
                (row["player_tag"], row["observed_at"], row["member_id"], row["clan_tag"],
                 row["player_name"], row["status"], json.dumps(row["fields"], separators=(",", ":")))
                for row in observations
            ])
            conn.commit()

    def read_window(self, *, after, before, selector, values):
        # Selectors are internal constants; every member-supplied value is bound.
        column = {"member_ids": "member_id", "account_tags": "player_tag", "clan_tags": "clan_tag"}[selector]
        placeholders = ",".join("?" for _ in values)
        with closing(sqlite3.connect(self.path, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(f"""SELECT * FROM snapshots
                WHERE observed_at >= ? AND observed_at < ? AND player_tag IN (
                    SELECT DISTINCT player_tag FROM snapshots WHERE observed_at >= ?
                    AND observed_at < ? AND {column} IN ({placeholders})
                ) ORDER BY player_tag, observed_at LIMIT 250001""", (after, before, after, before, *values)).fetchall()
        return [dict(row) for row in rows[:250000]], len(rows) > 250000
