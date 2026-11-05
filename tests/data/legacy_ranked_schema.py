"""The old ranked store's version-one tables, for building synthetic startup-copy sources."""


def create_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS snapshots (
        player_tag TEXT NOT NULL, observed_at TEXT NOT NULL,
        member_id INTEGER NOT NULL, clan_tag TEXT,
        player_name TEXT, status TEXT NOT NULL, fields_json TEXT NOT NULL,
        PRIMARY KEY (player_tag, observed_at)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS ranked_battles (
        battle_key TEXT PRIMARY KEY, player_tag TEXT NOT NULL, battle_at TEXT NOT NULL,
        battle_type TEXT NOT NULL, attack INTEGER NOT NULL, opponent_tag TEXT,
        member_id INTEGER NOT NULL, clan_tag TEXT, player_name TEXT,
        first_observed_at TEXT NOT NULL, last_observed_at TEXT NOT NULL,
        payload_json TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS battle_log_polls (
        player_tag TEXT NOT NULL, observed_at TEXT NOT NULL, member_id INTEGER NOT NULL,
        clan_tag TEXT, status TEXT NOT NULL, total_entries INTEGER,
        oldest_battle_at TEXT, newest_battle_at TEXT, skipped_entries INTEGER NOT NULL,
        poll_interval_seconds REAL NOT NULL, PRIMARY KEY (player_tag, observed_at)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS profile_health (
        player_tag TEXT PRIMARY KEY, missing INTEGER NOT NULL
    )""")
