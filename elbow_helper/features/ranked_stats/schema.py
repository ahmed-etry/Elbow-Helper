"""Version-one ranked schema, including the deployed battle upgrades."""

import sqlite3

from elbow_helper.infrastructure.persistence.migrations import SQLiteMigration
from elbow_helper.infrastructure.persistence.versioned import (
    VersionedSQLiteDatabase, validate_sqlite_shape,
)


def create_schema(conn: sqlite3.Connection) -> None:
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
    # A row also records that this tag has already had its 404 warning.
    conn.execute("""CREATE TABLE IF NOT EXISTS profile_health (
        player_tag TEXT PRIMARY KEY, missing INTEGER NOT NULL
    )""")


# store.append uses positional INSERTs for these three tables. All SELECT *
# reads use sqlite3.Row/dict; snapshots is inserted with an explicit column list.
_POSITIONAL_TABLES = frozenset({"ranked_battles", "battle_log_polls", "profile_health"})
_NULLABLE = {
    "snapshots": {"clan_tag", "player_name"},
    "ranked_battles": {"opponent_tag", "clan_tag", "player_name"},
    "battle_log_polls": {"clan_tag", "total_entries", "oldest_battle_at", "newest_battle_at"},
}


def validate_schema(conn: sqlite3.Connection, *, warn: bool = True) -> None:
    validate_sqlite_shape(
        conn, create_schema, positional_tables=_POSITIONAL_TABLES,
        required_nullable=_NULLABLE, warn=warn,
    )


def adopt_schema(conn: sqlite3.Connection) -> None:
    validate_sqlite_shape(
        conn, create_schema,
        optional_tables=frozenset({"ranked_battles", "battle_log_polls", "profile_health"}),
        positional_tables=_POSITIONAL_TABLES,
        required_nullable={**_NULLABLE, "ranked_battles": {"clan_tag", "player_name"}},
        warn=False,
    )
    create_schema(conn)
    validate_schema(conn, warn=False)


SCHEMA_VERSION = 1

DATABASE = VersionedSQLiteDatabase(
    "ranked", create_schema, adopt_schema,
    migrations=(SQLiteMigration(1, "ranked baseline", create_schema),),
    target_version=SCHEMA_VERSION, validate=validate_schema,
)
