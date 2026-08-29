"""SQLite schema and transactional helpers for achievements."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Optional

import discord

from .config import RUNTIME_CACHE_PRUNE_INTERVAL_SECONDS
from .config import RUNTIME_CACHE_RETENTION_DAYS

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


class AchievementsDatabaseMixin:
    def init_database(self):
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=20.0)
            cursor = conn.cursor()
            # Use WAL + NORMAL sync to reduce writer stalls under message load.
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA synchronous = NORMAL")
            cursor.execute("PRAGMA temp_store = MEMORY")

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

            conn.commit()

        except (sqlite3.Error, OSError):
            self.logger.exception("Error initializing database")
        finally:
            if conn:
                conn.close()

    def _ensure_user_stats_row(self, cursor: sqlite3.Cursor, user_id: int):
        """Ensure a stats row exists for the user before counter updates."""
        cursor.execute(
            'INSERT OR IGNORE INTO user_stats (user_id) VALUES (?)',
            (user_id,),
        )

    def _queue_post_commit_action(self, coro, *args, **kwargs) -> bool:
        """Schedule async side-effects to run only after commit succeeds."""
        queue = self._post_commit_actions_var.get()
        if queue is None:
            return False
        queue.append((coro, args, kwargs))
        return True

    def _maybe_prune_runtime_caches(self, now: Optional[datetime] = None):
        """Keep in-memory anti-spam caches bounded during long uptimes."""
        now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        now_ts = int(now_dt.timestamp())
        if (
            self._last_cache_prune_ts
            and (now_ts - self._last_cache_prune_ts) < RUNTIME_CACHE_PRUNE_INTERVAL_SECONDS
        ):
            return

        self._last_cache_prune_ts = now_ts
        cutoff_ts = now_ts - (RUNTIME_CACHE_RETENTION_DAYS * 86400)
        self._message_count_cooldowns = {
            user_id: ts
            for user_id, ts in self._message_count_cooldowns.items()
            if ts >= cutoff_ts
        }
        self._meme_cooldowns = {
            user_id: ts
            for user_id, ts in self._meme_cooldowns.items()
            if ts >= cutoff_ts
        }

        valid_days = {
            int((now_dt.date() - timedelta(days=offset)).strftime("%Y%m%d"))
            for offset in range(RUNTIME_CACHE_RETENTION_DAYS + 1)
        }
        self._emoji_daily_counts = {
            key: value
            for key, value in self._emoji_daily_counts.items()
            if key[1] in valid_days
        }
        self._reaction_daily_counts = {
            key: value
            for key, value in self._reaction_daily_counts.items()
            if key[1] in valid_days
        }
        self._meme_daily_counts = {
            key: value
            for key, value in self._meme_daily_counts.items()
            if key[1] in valid_days
        }
        self._active_channel_daily_sets = {
            key: value
            for key, value in self._active_channel_daily_sets.items()
            if key[1] in valid_days
        }

    async def cleanup_old_achievements(self):
        """Remove stale achievement rows that are no longer defined."""
        await self._retry_db_operation(self._cleanup_old_achievements_internal)

    async def _cleanup_old_achievements_internal(self, cursor):
        valid_achievement_ids = [ach[0] for ach in self.ALL_ACHIEVEMENTS]
        
        placeholders = ', '.join(["?" for _ in valid_achievement_ids])
        cursor.execute(f'''
            DELETE FROM user_achievements 
            WHERE achievement_id NOT IN ({placeholders})
        ''', valid_achievement_ids)
        
        cursor.execute(f'''
            DELETE FROM achievements 
            WHERE id NOT IN ({placeholders})
        ''', valid_achievement_ids)

    @staticmethod
    def _month_key(dt: Optional[datetime] = None) -> int:
        dt = dt or datetime.now(timezone.utc)
        return dt.year * 12 + dt.month

    async def _ensure_coin_row(self, cursor, user_id: int):
        cursor.execute('SELECT 1 FROM user_coins WHERE user_id = ?', (user_id,))
        if cursor.fetchone() is None:
            cursor.execute('INSERT INTO user_coins (user_id, balance, daily_msg_count) VALUES (?, ?, ?)', (user_id, 0, 0))

    async def _add_coins(self, cursor, user_id: int, amount: int, typ: str, reason: Optional[str], actor_id: Optional[int]):
        await self._ensure_coin_row(cursor, user_id)
        cursor.execute('SELECT balance FROM user_coins WHERE user_id = ?', (user_id,))
        balance = cursor.fetchone()[0]
        guild = self.bot.get_guild(self.GUILD_ID)
        if guild:
            member = guild.get_member(user_id)
            if member and self._is_leadership_any(member):
                return balance  # Leadership is excluded from coin economy
        new_balance = balance + amount
        cursor.execute('UPDATE user_coins SET balance = ? WHERE user_id = ?', (new_balance, user_id))
        cursor.execute('''
            INSERT INTO coin_transactions (user_id, amount, type, reason, actor_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, amount, typ, reason, actor_id, int(datetime.now(timezone.utc).timestamp())))
        return new_balance

    async def _set_meta(self, cursor, key: str, value: str):
        cursor.execute('INSERT OR REPLACE INTO economy_meta (key, value) VALUES (?, ?)', (key, value))

    async def _get_meta(self, cursor, key: str) -> Optional[str]:
        cursor.execute('SELECT value FROM economy_meta WHERE key = ?', (key,))
        row = cursor.fetchone()
        return row[0] if row else None

    @staticmethod
    def _parse_month_label(month_label: str) -> Optional[int]:
        try:
            dt = datetime.strptime(month_label, "%Y-%m")
            return dt.year * 12 + dt.month
        except ValueError:
            return None

    @staticmethod
    def _month_label_from_key(month_key: int) -> str:
        year = (month_key - 1) // 12
        month = month_key - (year * 12)
        return datetime(year, month, 1).strftime("%B %Y")

    async def _get_raffle_hub_state_internal(self, cursor) -> tuple[Optional[str], Optional[int]]:
        state = await self._get_meta(cursor, "raffle_hub_state")
        month_raw = await self._get_meta(cursor, "raffle_hub_month")
        month_key = int(month_raw) if month_raw and month_raw.isdigit() else None
        return state, month_key

    async def _set_raffle_hub_state_internal(self, cursor, state: str, month_key: int):
        await self._set_meta(cursor, "raffle_hub_state", state)
        await self._set_meta(cursor, "raffle_hub_month", str(month_key))

    def init_achievements(self):
        """Initialize achievement definitions"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        for achievement in self.ALL_ACHIEVEMENTS:
            cursor.execute('''
                INSERT OR REPLACE INTO achievements 
                (id, name, description, required_count, emoji)
                VALUES (?, ?, ?, ?, ?)
            ''', achievement[:5])
        
        conn.commit()

    async def _retry_db_operation(self, func, *args, **kwargs):
        """Retry DB work on lock contention and run queued side-effects only after commit."""
        result = None
        post_commit_actions = []
        async with self._db_lock:
            retries = 6
            for i in range(retries):
                conn = None
                queued_actions = []
                token = self._post_commit_actions_var.set(queued_actions)
                try:
                    conn = sqlite3.connect(self.db_path, timeout=0.0, check_same_thread=False)
                    cursor = conn.cursor()
                    cursor.execute("PRAGMA busy_timeout = 0")
                    cursor.execute("PRAGMA synchronous = NORMAL")
                    result = await func(cursor, *args, **kwargs)
                    await asyncio.to_thread(conn.commit)
                    # Defer Discord/network side-effects until the DB commit is durable.
                    post_commit_actions = queued_actions
                    break
                except sqlite3.OperationalError as e:
                    err = str(e).lower()
                    is_lock_error = "database is locked" in err or "database is busy" in err
                    if is_lock_error and i < retries - 1:
                        await asyncio.sleep(0.15 * (2 ** i))
                        continue
                    self.logger.error("DB operation failed (attempt %s/%s): %s", i + 1, retries, e)
                    raise
                finally:
                    self._post_commit_actions_var.reset(token)
                    if conn:
                        conn.close()

        # Run side-effects outside the DB lock to keep lock hold time minimal.
        for coro, action_args, action_kwargs in post_commit_actions:
            try:
                await coro(*action_args, **action_kwargs)
            except (discord.HTTPException, RuntimeError):
                self.logger.exception(
                    "Post-commit action failed: action=%s",
                    getattr(coro, "__name__", repr(coro)),
                )
        return result

    def get_achievement_details(self, achievement_id: str):
        """Retrieve achievement details from the database."""
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=20.0)
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, name, description, required_count, emoji
                FROM achievements
                WHERE id = ?
            ''', (achievement_id,))
            result = cursor.fetchone()
            return result
        except sqlite3.Error:
            self.logger.exception("Failed to fetch achievement details for %s", achievement_id)
            return None
        finally:
            if conn:
                conn.close()
    
