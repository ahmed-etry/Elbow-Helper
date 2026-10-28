"""Persistent player observations with historical ownership and clan membership."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .schema import DATABASE


class RankedStore:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self):
        DATABASE.initialize(self.path)

    def append(self, observations, *, battles=(), polls=(), profile_health=None):
        with closing(DATABASE.connect(self.path, timeout=30)) as conn:
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
            conn.executemany("""INSERT INTO profile_health VALUES (?, ?)
                ON CONFLICT(player_tag) DO UPDATE SET missing=excluded.missing""",
                list((profile_health or {}).items()))
            conn.commit()

    def profile_health(self, tags):
        """Read durable 404 warning and recovery state for the linked accounts."""
        with closing(DATABASE.connect(self.path, timeout=30)) as conn:
            return {tag: bool(row[0]) for tag in tags
                    if (row := conn.execute("SELECT missing FROM profile_health WHERE player_tag = ?",
                                            (tag,)).fetchone()) is not None}

    def read_battle_window(self, *, after, before, selector, values):
        column = {"member_ids": "member_id", "account_tags": "player_tag", "clan_tags": "clan_tag"}[selector]
        placeholders = ",".join("?" for _ in values)
        with closing(DATABASE.connect(self.path, timeout=30)) as conn:
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
        with closing(DATABASE.connect(self.path, timeout=30)) as conn:
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
        with closing(DATABASE.connect(self.path, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(f"""SELECT * FROM snapshots
                WHERE observed_at >= ? AND observed_at < ? AND player_tag IN (
                    SELECT DISTINCT player_tag FROM snapshots WHERE observed_at >= ?
                    AND observed_at < ? AND {column} IN ({placeholders})
                ) ORDER BY player_tag, observed_at LIMIT 250001""", (after, before, after, before, *values)).fetchall()
        return [dict(row) for row in rows[:250000]], len(rows) > 250000
