"""Version-one achievements schema and historical CWL reward adoption."""

import sqlite3

from elbow_helper.infrastructure.persistence.migrations import SQLiteMigration
from elbow_helper.infrastructure.persistence.versioned import (
    VersionedSQLiteDatabase, validate_sqlite_shape,
)

COIN_DB_INIT = [
    '''
    CREATE TABLE IF NOT EXISTS user_coins (
        user_id INTEGER PRIMARY KEY,
        balance INTEGER NOT NULL DEFAULT 0,
        last_ticket_month INTEGER,
        last_salary_month INTEGER,
        last_daily_ts INTEGER,
        manual_cwl_month INTEGER,
        manual_cwl_awarded INTEGER DEFAULT 0,
        manual_enc_month INTEGER,
        manual_enc_awarded INTEGER DEFAULT 0,
        daily_msg_day INTEGER,
        daily_msg_count INTEGER DEFAULT 0
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS coin_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount INTEGER NOT NULL,
        type TEXT NOT NULL,
        reason TEXT,
        actor_id INTEGER,
        created_at INTEGER NOT NULL
    )
    ''',
    '''
    CREATE INDEX IF NOT EXISTS coin_transactions_member_time_idx
    ON coin_transactions (user_id, created_at)
    ''',
    '''
    CREATE TABLE IF NOT EXISTS cwl_reward_grants (
        reason TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        reward_kind TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        PRIMARY KEY (reason, user_id, reward_kind)
    )
    ''',
    '''
    INSERT OR IGNORE INTO cwl_reward_grants
        (reason, user_id, reward_kind, created_at)
    SELECT reason, user_id, 'coins', created_at
    FROM coin_transactions
    WHERE type = 'bonus_cwl' AND reason IS NOT NULL
    ''',
    '''
    CREATE TABLE IF NOT EXISTS raffle_tickets (
        month_key INTEGER NOT NULL,
        user_id INTEGER PRIMARY KEY
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS raffle_winners (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        month_key INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        drawn_at INTEGER NOT NULL,
        draw_type TEXT NOT NULL DEFAULT 'draw',
        reroll_of INTEGER,
        is_active INTEGER NOT NULL DEFAULT 1,
        UNIQUE (month_key, user_id)
    )
    ''',
    '''
    CREATE TABLE IF NOT EXISTS economy_meta (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    ''',
]


def create_schema(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS achievements (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            required_count INTEGER,
            emoji TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_achievements (
            user_id INTEGER NOT NULL,
            achievement_id TEXT NOT NULL,
            completed_date INTEGER NOT NULL,
            PRIMARY KEY (user_id, achievement_id),
            FOREIGN KEY (achievement_id) REFERENCES achievements(id)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_stats (
            user_id INTEGER PRIMARY KEY,
            message_count INTEGER DEFAULT 0,
            emoji_count INTEGER DEFAULT 0,
            reaction_count INTEGER DEFAULT 0,
            voice_hours REAL DEFAULT 0.0,
            voice_total_seconds INTEGER DEFAULT 0,
            silent_voice_seconds INTEGER DEFAULT 0,
            role_pings INTEGER DEFAULT 0,
            meme_posts INTEGER DEFAULT 0,
            hibernation_count INTEGER DEFAULT 0,
            role_changes_count INTEGER DEFAULT 0,
            opinion_uses_count INTEGER DEFAULT 0,
            member_status_changes_count INTEGER DEFAULT 0,
            clan_changes_count INTEGER DEFAULT 0,
            clan_transfer_count INTEGER DEFAULT 0,
            voice_unmuted INTEGER DEFAULT 0,
            voice_never_unmuted INTEGER DEFAULT 1,
            active_channels TEXT DEFAULT '',
            last_voice_join INTEGER DEFAULT 0,
            last_activity_date INTEGER DEFAULT 0,
            activity_streak INTEGER DEFAULT 0,
            weekly_activity_count INTEGER DEFAULT 0,
            monthly_activity_count INTEGER DEFAULT 0,
            early_bird_count INTEGER DEFAULT 0,
            night_owl_count INTEGER DEFAULT 0,
            early_bird_last_local_day INTEGER DEFAULT 0,
            night_owl_last_local_day INTEGER DEFAULT 0
        )
    ''')


    for stmt in COIN_DB_INIT:
        cursor.execute(stmt)


# _ensure_user_stats_row and progress insert only user_id. Direct SQL/Python
# arithmetic requires zero; readers using `value or 0` also support NULL.
_REQUIRED_DEFAULTS = {
    "user_stats": {
        **dict.fromkeys(("emoji_count", "voice_hours", "voice_total_seconds",
                         "silent_voice_seconds", "voice_unmuted",
                         "early_bird_count", "night_owl_count"), (0,)),
        **dict.fromkeys(("message_count", "reaction_count", "role_pings", "meme_posts",
                         "clan_transfer_count", "last_voice_join", "last_activity_date",
                         "activity_streak", "weekly_activity_count", "monthly_activity_count",
                         "early_bird_last_local_day", "night_owl_last_local_day"), (0, None)),
        "active_channels": ("", None),
    },
    # _ensure_coin_row supplies balance and daily_msg_count. Omitted month/day
    # markers must mean 'never'; manual counts reset when their month differs.
    "user_coins": dict.fromkeys(("last_ticket_month", "last_salary_month", "last_daily_ts",
                                 "manual_cwl_month", "manual_enc_month", "daily_msg_day"), (None, 0)),
}
_OMITTED_COLUMNS = {
    "user_stats": {
        "message_count", "emoji_count", "reaction_count", "voice_hours", "voice_total_seconds",
        "silent_voice_seconds", "role_pings", "meme_posts", "hibernation_count", "role_changes_count",
        "opinion_uses_count", "member_status_changes_count", "clan_changes_count", "clan_transfer_count",
        "voice_unmuted", "voice_never_unmuted", "active_channels", "last_voice_join", "last_activity_date",
        "activity_streak", "weekly_activity_count", "monthly_activity_count", "early_bird_count",
        "night_owl_count", "early_bird_last_local_day", "night_owl_last_local_day",
    },
    "user_coins": {"last_ticket_month", "last_salary_month", "last_daily_ts", "manual_cwl_month",
                   "manual_cwl_awarded", "manual_enc_month", "manual_enc_awarded", "daily_msg_day"},
    "coin_transactions": {"id"}, "raffle_winners": {"id"},
}


def validate_schema(conn: sqlite3.Connection, *, warn: bool = True, legacy: bool = False) -> None:
    # Coin history and raffle draws order by generated id. Every INSERT names
    # its columns, and missing FK enforcement is harmless (foreign_keys is OFF).
    validate_sqlite_shape(
        conn, create_schema,
        optional_tables=frozenset({"cwl_reward_grants"}) if legacy else frozenset(),
        required_defaults=_REQUIRED_DEFAULTS,
        required_nullable={"coin_transactions": {"reason", "actor_id"}, "raffle_winners": {"reroll_of"}},
        generated_ids={"coin_transactions": "id", "raffle_winners": "id"},
        omitted_columns=_OMITTED_COLUMNS, warn=warn,
    )


def adopt_schema(conn: sqlite3.Connection) -> None:
    validate_schema(conn, warn=False, legacy=True)
    # Keep the deployed reward-ledger backfill; no other stored values change.
    for statement in COIN_DB_INIT:
        conn.execute(statement)
    validate_schema(conn, warn=False)


SCHEMA_VERSION = 1

DATABASE = VersionedSQLiteDatabase(
    "achievements", create_schema, adopt_schema,
    migrations=(SQLiteMigration(1, "achievements baseline", create_schema),),
    target_version=SCHEMA_VERSION, validate=validate_schema,
)
