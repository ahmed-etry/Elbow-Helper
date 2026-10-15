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
            conn.execute("""CREATE TABLE IF NOT EXISTS ranked_battles (
                battle_key TEXT PRIMARY KEY, player_tag TEXT NOT NULL, battle_at TEXT NOT NULL,
                battle_type TEXT NOT NULL, attack INTEGER NOT NULL, opponent_tag TEXT,
                member_id INTEGER NOT NULL, clan_tag TEXT, player_name TEXT,
                first_observed_at TEXT NOT NULL, last_observed_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )""")
            columns = conn.execute("PRAGMA table_info(ranked_battles)").fetchall()
            if any(column[1] == "opponent_tag" and column[3] for column in columns):
                # SQLite cannot drop NOT NULL in place. Rebuild in one transaction,
                # preserving battle keys, payloads and first-observed associations.
                conn.execute("BEGIN")
                conn.execute("ALTER TABLE ranked_battles RENAME TO ranked_battles_old")
                conn.execute("""CREATE TABLE ranked_battles (
                    battle_key TEXT PRIMARY KEY, player_tag TEXT NOT NULL, battle_at TEXT NOT NULL,
                    battle_type TEXT NOT NULL, attack INTEGER NOT NULL, opponent_tag TEXT,
                    member_id INTEGER NOT NULL, clan_tag TEXT, player_name TEXT,
                    first_observed_at TEXT NOT NULL, last_observed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )""")
                conn.execute("INSERT INTO ranked_battles SELECT * FROM ranked_battles_old")
                conn.execute("DROP TABLE ranked_battles_old")
            conn.execute("CREATE INDEX IF NOT EXISTS battles_time ON ranked_battles(battle_at, player_tag)")
            conn.execute("CREATE INDEX IF NOT EXISTS battles_member ON ranked_battles(member_id, battle_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS battles_clan ON ranked_battles(clan_tag, battle_at)")
            conn.execute("""CREATE TABLE IF NOT EXISTS battle_log_polls (
                player_tag TEXT NOT NULL, observed_at TEXT NOT NULL, member_id INTEGER NOT NULL,
                clan_tag TEXT, status TEXT NOT NULL, total_entries INTEGER,
                oldest_battle_at TEXT, newest_battle_at TEXT, skipped_entries INTEGER NOT NULL,
                poll_interval_seconds REAL NOT NULL, PRIMARY KEY (player_tag, observed_at)
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS polls_member ON battle_log_polls(member_id, observed_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS polls_clan ON battle_log_polls(clan_tag, observed_at)")
            conn.commit()

    def append(self, observations, *, battles=(), polls=()):
        with closing(sqlite3.connect(self.path, timeout=30)) as conn:
            conn.executemany("""INSERT OR IGNORE INTO snapshots
                (player_tag, observed_at, member_id, clan_tag, player_name, status, fields_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)""", [
                (row["player_tag"], row["observed_at"], row["member_id"], row["clan_tag"],
                 row["player_name"], row["status"], json.dumps(row["fields"], separators=(",", ":")))
                for row in observations
            ])
            conn.executemany("""INSERT INTO ranked_battles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(battle_key) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    last_observed_at=MAX(ranked_battles.last_observed_at, excluded.last_observed_at)""", [
                (row["battle_key"], row["player_tag"], row["battle_at"], row["battle_type"],
                 row["attack"], row["opponent_tag"], row["member_id"], row["clan_tag"], row["player_name"],
                 row["observed_at"], row["observed_at"], json.dumps(row["payload"], separators=(",", ":")))
                for row in battles
            ])
            conn.executemany("""INSERT OR IGNORE INTO battle_log_polls
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", [
                (row["player_tag"], row["observed_at"], row["member_id"], row["clan_tag"], row["status"],
                 row["total_entries"], row["oldest_battle_at"], row["newest_battle_at"],
                 row["skipped_entries"], row["poll_interval_seconds"])
                for row in polls
            ])
            conn.commit()

    def read_battle_window(self, *, after, before, selector, values):
        column = {"member_ids": "member_id", "account_tags": "player_tag", "clan_tags": "clan_tag"}[selector]
        placeholders = ",".join("?" for _ in values)
        with closing(sqlite3.connect(self.path, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            battles = conn.execute(f"""SELECT * FROM ranked_battles
                WHERE battle_at >= ? AND battle_at < ? AND {column} IN ({placeholders})
                ORDER BY player_tag, battle_at LIMIT 250001""", (after, before, *values)).fetchall()
            polls = conn.execute(f"""SELECT * FROM battle_log_polls
                WHERE observed_at >= ? AND observed_at < ? AND {column} IN ({placeholders})
                ORDER BY player_tag, observed_at LIMIT 250001""", (after, before, *values)).fetchall()
        return ([dict(row) for row in battles[:250000]], [dict(row) for row in polls[:250000]],
                len(battles) > 250000 or len(polls) > 250000)

    def latest_collection(self, tags):
        """Restore per-account cooldowns without reading the full history."""
        with closing(sqlite3.connect(self.path, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            latest = {}
            for tag in tags:
                profile = conn.execute("""SELECT * FROM snapshots WHERE player_tag = ?
                    ORDER BY observed_at DESC LIMIT 1""", (tag,)).fetchone()
                poll = conn.execute("""SELECT * FROM battle_log_polls WHERE player_tag = ?
                    ORDER BY observed_at DESC LIMIT 1""", (tag,)).fetchone()
                latest[tag] = (dict(profile) if profile else None, dict(poll) if poll else None)
        return latest

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
