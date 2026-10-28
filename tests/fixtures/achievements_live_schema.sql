-- Live achievements DDL from the 2026-10-28 backup. No member data.

CREATE TABLE achievements (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    required_count INTEGER,
                    emoji TEXT,
                    leadership_only BOOLEAN DEFAULT FALSE
                );

CREATE TABLE user_achievements (
                    user_id INTEGER NOT NULL,
                    achievement_id TEXT NOT NULL,
                    completed_date INTEGER NOT NULL,
                    PRIMARY KEY (user_id, achievement_id),
                    FOREIGN KEY (achievement_id) REFERENCES achievements(id)
                );

CREATE TABLE user_stats (
                    user_id INTEGER PRIMARY KEY,
                    message_count INTEGER DEFAULT 0,
                    emoji_count INTEGER DEFAULT 0,
                    reaction_count INTEGER DEFAULT 0,
                    voice_hours REAL DEFAULT 0.0,
                    role_pings INTEGER DEFAULT 0,
                    meme_posts INTEGER DEFAULT 0,
                    hibernation_count INTEGER DEFAULT 0,
                    role_changes_count INTEGER DEFAULT 0,
                    opinion_uses_count INTEGER DEFAULT 0,
                    member_status_changes_count INTEGER DEFAULT 0,
                    clan_changes_count INTEGER DEFAULT 0,
                    voice_unmuted INTEGER DEFAULT 0,
                    active_channels TEXT DEFAULT '',
                    last_voice_join INTEGER DEFAULT 0,
                    last_activity_date INTEGER DEFAULT 0,
                    activity_streak INTEGER DEFAULT 0,
                    weekly_activity_count INTEGER DEFAULT 0,
                    monthly_activity_count INTEGER DEFAULT 0,
                    early_bird_count INTEGER DEFAULT 0,
                    night_owl_count INTEGER DEFAULT 0
                , early_bird_last_local_day INTEGER DEFAULT 0, night_owl_last_local_day INTEGER DEFAULT 0, voice_total_seconds INTEGER DEFAULT 0, silent_voice_seconds INTEGER DEFAULT 0, voice_never_unmuted INTEGER DEFAULT 1, clan_transfer_count INTEGER DEFAULT 0);

CREATE TABLE user_coins (
        user_id INTEGER PRIMARY KEY,
        balance INTEGER NOT NULL DEFAULT 0,
        last_ticket_month INTEGER,
        last_salary_month INTEGER,
        last_daily_ts INTEGER
    , manual_cwl_month INTEGER, manual_cwl_awarded INTEGER DEFAULT 0, manual_enc_month INTEGER, manual_enc_awarded INTEGER DEFAULT 0, daily_msg_day INTEGER, daily_msg_count INTEGER DEFAULT 0);

CREATE TABLE coin_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount INTEGER NOT NULL,
        type TEXT NOT NULL,
        reason TEXT,
        actor_id INTEGER,
        created_at INTEGER NOT NULL
    );

CREATE TABLE raffle_tickets (
        month_key INTEGER NOT NULL,
        user_id INTEGER PRIMARY KEY
    );

CREATE TABLE "raffle_winners_old" (
        month_key INTEGER PRIMARY KEY,
        user_id INTEGER,
        drawn_at INTEGER NOT NULL
    );

CREATE TABLE economy_meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );

CREATE TABLE raffle_winners (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month_key INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                drawn_at INTEGER NOT NULL,
                draw_type TEXT NOT NULL DEFAULT 'draw',
                reroll_of INTEGER,
                is_active INTEGER NOT NULL DEFAULT 1,
                UNIQUE (month_key, user_id)
            );

CREATE TABLE cwl_reward_grants (
        reason TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        reward_kind TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        PRIMARY KEY (reason, user_id, reward_kind)
    );

CREATE INDEX coin_transactions_member_time_idx
    ON coin_transactions (user_id, created_at)
    ;
