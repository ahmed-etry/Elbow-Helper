"""Background loops, listeners, and stat tracking for achievements."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Optional

import discord
from discord.ext import commands
from discord.ext import tasks

from elbow_helper.configuration.channels import MEMES
from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.roles import ELDER_ROLE_ID
from elbow_helper.configuration.roles import TRIAL_ROLE_ID
from elbow_helper.configuration.timezones import REGION_ROLE_TIMEZONES
from elbow_helper.infrastructure.time import resolve_timezone

from .config import ACTIVE_CHANNEL_DAILY_LIMIT
from .config import ACTIVE_CHANNEL_MIN_CHARS
from .config import CLAN_ROLE_CHANGE_DELAY_SECONDS
from .config import EMOJI_DAILY_LIMIT
from .config import MEME_COOLDOWN_SECONDS
from .config import MEME_DAILY_LIMIT
from .config import MESSAGE_COUNT_COOLDOWN_SECONDS
from .config import RANDOM_CRIT_TRIGGER_RE
from .config import REACTION_DAILY_LIMIT

NON_UTILITY_CLAN_MEMBER_ROLE_IDS = {
    int(clan.member_role_id)
    for clan in CLANS.values()
    if clan.member_role_id is not None and not clan.is_utility
}

class AchievementTrackingMixin:
    @tasks.loop(hours=24)
    async def check_time_achievements(self):
        """Periodically check time-based achievements for all members (silent for existing members)"""
        try:
            guild = self.bot.get_guild(self.GUILD_ID)
            if not guild:
                return
            
            for member in guild.members:
                if not member.bot:
                    await self.check_time_based_achievements(member.id, announce=False)
                    
        except (discord.HTTPException, sqlite3.Error, RuntimeError):
            self.logger.exception("Error in check_time_achievements")

    @check_time_achievements.before_loop
    async def before_check_time_achievements(self):
        await self.bot.wait_until_ready()
    
    @tasks.loop(count=1)
    async def initial_achievement_check(self):
        """Initial silent achievement check for existing members"""
        try:
            await asyncio.sleep(10)  # Wait for bot to be ready
            guild = self.bot.get_guild(self.GUILD_ID)
            if not guild:
                return
            
            for member in guild.members:
                if not member.bot:
                    await self.check_time_based_achievements(member.id, announce=False)

        except (discord.HTTPException, sqlite3.Error, RuntimeError):
            self.logger.exception("Error in initial_achievement_check")

    @initial_achievement_check.before_loop
    async def before_initial_achievement_check(self):
        await self.bot.wait_until_ready()
    
    @tasks.loop(hours=24)
    async def cleanup_database(self):
        """Clean up stats/achievement rows for users who left the server."""
        conn = None
        try:
            guild = self.bot.get_guild(self.GUILD_ID)
            if not guild:
                return

            current_member_ids = {member.id for member in guild.members}
            
            conn = sqlite3.connect(self.db_path, timeout=20.0)
            cursor = conn.cursor()

            cursor.execute('SELECT DISTINCT user_id FROM user_stats')
            db_user_ids = {row[0] for row in cursor.fetchall()}

            left_user_ids = db_user_ids - current_member_ids
            
            if left_user_ids:
                self.logger.info("Starting DB cleanup for %s departed users", len(left_user_ids))

                for user_id in left_user_ids:
                    cursor.execute('DELETE FROM user_stats WHERE user_id = ?', (user_id,))
                    cursor.execute('DELETE FROM user_achievements WHERE user_id = ?', (user_id,))
                
                conn.commit()
                self.logger.info("Cleaned up data for %s users", len(left_user_ids))
            else:
                self.logger.info("DB cleanup skipped: no departed users found")
            
        except sqlite3.Error:
            self.logger.exception("Error in cleanup_database")
        finally:
            if conn:
                conn.close()

    @cleanup_database.before_loop
    async def before_cleanup_database(self):
        await self.bot.wait_until_ready()
    

    @commands.Cog.listener()
    async def on_member_join(self, member):
        """Track member joins"""
        await self.check_join_achievements(member)



    @commands.Cog.listener()
    async def on_member_remove(self, member):
        """Clean up data when member leaves"""
        try:
            await self._retry_db_operation(self._member_remove_cleanup_internal, member.id)
            self.logger.info("Cleaned up data for %s who left the server", member.display_name)
        except sqlite3.Error:
            self.logger.exception("Failed member cleanup for %s", member.display_name)

    async def _member_remove_cleanup_internal(self, cursor: sqlite3.Cursor, user_id: int):
        """Delete per-user stats/achievements for a departed member."""
        cursor.execute('DELETE FROM user_stats WHERE user_id = ?', (user_id,))
        cursor.execute('DELETE FROM user_achievements WHERE user_id = ?', (user_id,))
    

    
    @commands.Cog.listener()
    async def on_message(self, message):
        """Track message activity"""
        if message.author.bot:
            return
        
        await self.check_message_achievements(message)
        await self._maybe_grant_daily(message)
        try:
            mentioned_ids = {m.id for m in message.mentions if not m.bot and m.id != message.author.id}
            for uid in mentioned_ids:
                await self.track_role_ping(uid)
        except (AttributeError, TypeError):
            self.logger.exception("Error tracking role ping")
    
    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
        """Track reaction activity"""
        if user.bot:
            return

        await self.check_reaction_achievements(reaction, user)
    
    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        """Observe edits; reaction increments are counted in on_reaction_add."""
        if before.author.bot:
            return

        if len(after.reactions) > len(before.reactions):
            return
    
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Track voice activity"""
        if member.bot:
            return
        
        await self.check_voice_achievements(member, before, after)
    
    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        """Track role changes"""
        if before.roles == after.roles:
            return

        await self.check_role_achievements(before, after)
        self._queue_clan_role_change_check(before, after)
    
    async def check_join_achievements(self, member):
        """Check join-related achievements"""
        await self.check_time_based_achievements(member.id, announce=False)

    
    async def check_time_based_achievements(self, user_id: int, announce: bool = True):
        """Check time-based achievements (One of Us, Veteran) using Discord's join date"""
        guild = self.bot.get_guild(self.GUILD_ID)
        member = guild.get_member(user_id)
        
        if not member:
            return
        
        join_date = member.joined_at
        if not join_date:
            return
        
        current_time = datetime.now(timezone.utc)
        days_in_server = (current_time - join_date).days
        
        if days_in_server >= 30:
            await self.award_achievement(user_id, "one_of_us", announce=announce)
        
        if days_in_server >= 365:
            await self.award_achievement(user_id, "veteran", announce=announce)
    
    async def check_message_achievements(self, message):
        """Check message-related achievements"""
        await self._retry_db_operation(self._check_message_achievements_internal, message)
    
    async def _check_message_achievements_internal(self, cursor, message):
        user_id = message.author.id
        self._maybe_prune_runtime_caches(message.created_at)
        
        cursor.execute('''
            INSERT OR IGNORE INTO user_stats (user_id, message_count, emoji_count, reaction_count, 
                                            voice_hours, voice_total_seconds, silent_voice_seconds, role_pings, meme_posts, hibernation_count, 
                                            role_changes_count, opinion_uses_count, member_status_changes_count, 
                                            clan_changes_count, voice_unmuted, voice_never_unmuted, active_channels, last_voice_join,
                                            last_activity_date, activity_streak, weekly_activity_count, 
                                            monthly_activity_count, early_bird_count, night_owl_count)
            VALUES (?, 0, 0, 0, 0.0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, '', 0, 0, 0, 0, 0, 0, 0)
        ''', (user_id,))

        cursor.execute('SELECT message_count FROM user_stats WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        current_message_count = result[0] or 0

        can_count_message = True
        msg_ts = int(message.created_at.replace(tzinfo=timezone.utc).timestamp())
        last_ts = self._message_count_cooldowns.get(user_id)
        if last_ts is not None and (msg_ts - last_ts) < MESSAGE_COUNT_COOLDOWN_SECONDS:
            can_count_message = False
        else:
            self._message_count_cooldowns[user_id] = msg_ts

        if can_count_message:
            new_message_count = current_message_count + 1
            cursor.execute('UPDATE user_stats SET message_count = ? WHERE user_id = ?', (new_message_count, user_id))
            self.logger.debug(
                "Message count updated: user_id=%s channel_id=%s %s->%s",
                user_id,
                message.channel.id,
                current_message_count,
                new_message_count,
            )

        emoji_count = len(re.findall(r'<:\w*:\d*>', message.content)) + len(re.findall(r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF]', message.content))
        if emoji_count > 0:
            day_key = int(message.created_at.replace(tzinfo=timezone.utc).strftime("%Y%m%d"))
            current_daily = self._emoji_daily_counts.get((user_id, day_key), 0)
            remaining = max(0, EMOJI_DAILY_LIMIT - current_daily)
            if remaining > 0:
                to_add = min(emoji_count, remaining)
                self._emoji_daily_counts[(user_id, day_key)] = current_daily + to_add
                cursor.execute('UPDATE user_stats SET emoji_count = emoji_count + ? WHERE user_id = ?', (to_add, user_id))

        if message.channel.id == MEMES:
            has_attachment = bool(message.attachments)
            has_link = bool(re.search(r"https?://", message.content or ""))
            if has_attachment or has_link:
                msg_ts = int(message.created_at.replace(tzinfo=timezone.utc).timestamp())
                last_ts = self._meme_cooldowns.get(user_id)
                if last_ts is None or (msg_ts - last_ts) >= MEME_COOLDOWN_SECONDS:
                    day_key = int(message.created_at.replace(tzinfo=timezone.utc).strftime("%Y%m%d"))
                    current_daily = self._meme_daily_counts.get((user_id, day_key), 0)
                    if current_daily < MEME_DAILY_LIMIT:
                        self._meme_cooldowns[user_id] = msg_ts
                        self._meme_daily_counts[(user_id, day_key)] = current_daily + 1
                        cursor.execute('SELECT meme_posts FROM user_stats WHERE user_id = ?', (user_id,))
                        result = cursor.fetchone()
                        meme_posts = (result[0] or 0) + 1
                        cursor.execute('UPDATE user_stats SET meme_posts = ? WHERE user_id = ?', (meme_posts, user_id))

                        if meme_posts >= 25:
                            await self.award_achievement(user_id, "meme_dealer", cursor=cursor)

        if len(message.content or "") >= ACTIVE_CHANNEL_MIN_CHARS:
            await self._track_active_channels_internal(cursor, user_id, message.channel.id, message.created_at)

        await self._track_consistency_achievements_internal(cursor, user_id, message.created_at, message.author)

        if can_count_message:
            if new_message_count >= 100:
                await self.award_achievement(user_id, "chatterbox", cursor=cursor)
            if new_message_count >= 1000:
                await self.award_achievement(user_id, "keyboard_warrior", cursor=cursor)
        if len(message.content) > 500:
            await self.award_achievement(user_id, "storyteller", cursor=cursor)

        if RANDOM_CRIT_TRIGGER_RE.search(message.content or ""):
            await self.award_achievement(user_id, "random_crit", announce=False, cursor=cursor)

        cursor.execute('SELECT emoji_count FROM user_stats WHERE user_id = ?', (user_id,))
        current_total_emoji_count = cursor.fetchone()[0] or 0
        if current_total_emoji_count >= 50:
            await self.award_achievement(user_id, "emoji_enthusiast", cursor=cursor)
    
    async def check_reaction_achievements(self, reaction, user):
        """Check reaction-related achievements"""
        await self._retry_db_operation(self._check_reaction_achievements_internal, reaction, user)

    async def _check_reaction_achievements_internal(self, cursor, reaction, user):
        user_id = user.id
        self._maybe_prune_runtime_caches()
        
        cursor.execute('''
            INSERT OR IGNORE INTO user_stats (user_id, message_count, emoji_count, reaction_count, 
                                            voice_hours, voice_total_seconds, silent_voice_seconds, role_pings, meme_posts, hibernation_count, 
                                            role_changes_count, opinion_uses_count, member_status_changes_count, 
                                            clan_changes_count, voice_unmuted, voice_never_unmuted, active_channels, last_voice_join,
                                            last_activity_date, activity_streak, weekly_activity_count, 
                                            monthly_activity_count, early_bird_count, night_owl_count)
            VALUES (?, 0, 0, 0, 0.0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, '', 0, 0, 0, 0, 0, 0, 0)
        ''', (user_id,))

        message_obj = getattr(reaction, "message", None)
        author = getattr(message_obj, "author", None)
        if author is not None and getattr(author, "id", None) == user_id:
            return

        day_key = int(datetime.now(timezone.utc).strftime("%Y%m%d"))
        current_daily = self._reaction_daily_counts.get((user_id, day_key), 0)
        if current_daily >= REACTION_DAILY_LIMIT:
            return
        self._reaction_daily_counts[(user_id, day_key)] = current_daily + 1

        cursor.execute('SELECT reaction_count FROM user_stats WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        current_reaction_count = result[0] or 0

        new_reaction_count = current_reaction_count + 1
        cursor.execute('UPDATE user_stats SET reaction_count = ? WHERE user_id = ?', (new_reaction_count, user_id))

        if new_reaction_count >= 100:
            await self.award_achievement(user_id, "react_lord", cursor=cursor)
    
    async def check_voice_achievements(self, member, before, after):
        """Check voice-related achievements"""
        await self._retry_db_operation(self._check_voice_achievements_internal, member, before, after)
    
    async def _check_voice_achievements_internal(self, cursor, member, before, after):
        user_id = member.id

        cursor.execute('''
            INSERT OR IGNORE INTO user_stats (user_id, message_count, emoji_count, reaction_count, 
                                            voice_hours, voice_total_seconds, silent_voice_seconds, role_pings, meme_posts, hibernation_count, 
                                            role_changes_count, opinion_uses_count, member_status_changes_count, 
                                            clan_changes_count, voice_unmuted, voice_never_unmuted, active_channels, last_voice_join,
                                            last_activity_date, activity_streak, weekly_activity_count, 
                                            monthly_activity_count, early_bird_count, night_owl_count)
            VALUES (?, 0, 0, 0, 0.0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, '', 0, 0, 0, 0, 0, 0, 0)
        ''', (user_id,))

        if not before.channel and after.channel:
            await self.award_achievement(user_id, "mic_check", cursor=cursor)

            if len(after.channel.members) >= 5:
                await self.award_achievement(user_id, "party_animal", cursor=cursor)

            cursor.execute('''
                UPDATE user_stats 
                SET last_voice_join = ? 
                WHERE user_id = ?
            ''', (int(datetime.now(timezone.utc).timestamp()), user_id))

            cursor.execute('''
                UPDATE user_stats 
                SET voice_unmuted = ? 
                WHERE user_id = ?
            ''', (0, user_id))

        elif before.channel and not after.channel:
            cursor.execute('SELECT last_voice_join, voice_unmuted FROM user_stats WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            if result and result[0]:
                last_join_time = int(result[0])
                session_unmutes = int(result[1] or 0)
                session_seconds = max(
                    0,
                    int(
                        (datetime.now(timezone.utc) - datetime.fromtimestamp(last_join_time, tz=timezone.utc)).total_seconds()
                    ),
                )
                session_hours = session_seconds / 3600.0

                cursor.execute('''
                    UPDATE user_stats 
                    SET voice_hours = voice_hours + ?,
                        voice_total_seconds = voice_total_seconds + ?
                    WHERE user_id = ?
                ''', (session_hours, session_seconds, user_id))
                if session_unmutes == 0 and session_seconds > 0:
                    cursor.execute(
                        '''
                        UPDATE user_stats
                        SET silent_voice_seconds = silent_voice_seconds + ?
                        WHERE user_id = ?
                        ''',
                        (session_seconds, user_id),
                    )

                cursor.execute(
                    'SELECT voice_hours, voice_total_seconds, silent_voice_seconds FROM user_stats WHERE user_id = ?',
                    (user_id,),
                )
                updated_voice_hours, updated_voice_seconds, updated_silent_seconds = cursor.fetchone()
                updated_voice_hours = float(updated_voice_hours or 0.0)
                updated_voice_seconds = int(updated_voice_seconds or 0)
                updated_silent_seconds = int(updated_silent_seconds or 0)
                cursor.execute('SELECT required_count FROM achievements WHERE id = ?', ("silent_lurker",))
                silent_required_row = cursor.fetchone()
                silent_required_hours = int(silent_required_row[0]) if silent_required_row and silent_required_row[0] else 10
                if updated_voice_hours >= 24:
                    await self.award_achievement(user_id, "marathoner", cursor=cursor)
                if updated_silent_seconds >= (silent_required_hours * 3600):
                    await self.award_achievement(user_id, "silent_lurker", cursor=cursor)

            cursor.execute('''
                UPDATE user_stats 
                SET last_voice_join = ? 
                WHERE user_id = ?
            ''', (0, user_id))

        elif before.channel and after.channel and before.self_mute != after.self_mute:
            if not after.self_mute:  # User unmuted
                cursor.execute('''
                    UPDATE user_stats 
                    SET voice_unmuted = voice_unmuted + 1,
                        voice_never_unmuted = 0
                    WHERE user_id = ?
                ''', (user_id,))
    
    async def check_role_achievements(self, before, after):
        """Check role-related achievements"""
        had_trial_before = any(role.id == TRIAL_ROLE_ID for role in before.roles)
        has_trial_after = any(role.id == TRIAL_ROLE_ID for role in after.roles)
        
        if not had_trial_before and has_trial_after:
            await self.award_achievement(after.id, "fresh_recruit")
            self.logger.info("Awarded Fresh Recruit achievement to %s", after.display_name)
        
        rank_roles_before = [role.id for role in before.roles if role.id == ELDER_ROLE_ID]
        rank_roles_after = [role.id for role in after.roles if role.id == ELDER_ROLE_ID]
        
        if len(rank_roles_before) == 0 and len(rank_roles_after) > 0:
            await self.award_achievement(after.id, "promoted")

    def _queue_clan_role_change_check(self, before: discord.Member, after: discord.Member) -> None:
        clan_roles_before = {role.id for role in before.roles if role.id in NON_UTILITY_CLAN_MEMBER_ROLE_IDS}
        clan_roles_after = {role.id for role in after.roles if role.id in NON_UTILITY_CLAN_MEMBER_ROLE_IDS}
        if clan_roles_before == clan_roles_after:
            return

        self._pending_clan_role_baselines.setdefault(after.id, self._effective_non_utility_clan_role_id(before.roles))
        existing = self._clan_role_change_tasks.get(after.id)
        if existing and not existing.done():
            existing.cancel()

        task = asyncio.create_task(self._finalize_clan_role_change(after.guild.id, after.id))
        self._clan_role_change_tasks[after.id] = task

    async def _finalize_clan_role_change(self, guild_id: int, user_id: int) -> None:
        try:
            await asyncio.sleep(CLAN_ROLE_CHANGE_DELAY_SECONDS)
            guild = self.bot.get_guild(guild_id)
            if not guild:
                return

            member = guild.get_member(user_id)
            baseline_role_id = self._pending_clan_role_baselines.pop(user_id, None)
            if not member or baseline_role_id is None:
                return

            final_role_id = self._effective_non_utility_clan_role_id(member.roles)
            if baseline_role_id is not None and final_role_id is not None and final_role_id != baseline_role_id:
                await self.track_clan_hopper(user_id)
        except asyncio.CancelledError:
            raise
        finally:
            current = self._clan_role_change_tasks.get(user_id)
            if current is asyncio.current_task():
                self._clan_role_change_tasks.pop(user_id, None)

    def _effective_non_utility_clan_role_id(self, roles) -> Optional[int]:
        clan_roles = [role for role in roles if role.id in NON_UTILITY_CLAN_MEMBER_ROLE_IDS]
        if not clan_roles:
            return None
        return max(clan_roles, key=lambda role: (role.position, role.id)).id
    
    async def track_hibernation_survivor(self, user_id: int):
        """Track hibernation survivor achievement"""
        await self.award_achievement(user_id, "hibernation_survivor")
    
    async def track_role_ping(self, user_id: int):
        """Track user mentions for Ping Collector achievement."""
        await self._retry_db_operation(self._track_role_ping_internal, user_id)
    
    async def _track_role_ping_internal(self, cursor, user_id: int):
        self._ensure_user_stats_row(cursor, user_id)
        cursor.execute('SELECT role_pings FROM user_stats WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        role_pings = (result[0] or 0) + 1 if result else 1
        
        cursor.execute('UPDATE user_stats SET role_pings = ? WHERE user_id = ?', (role_pings, user_id))
    
        if role_pings >= 25:
            await self.award_achievement(user_id, "ping_collector", cursor=cursor)
    
    async def track_clan_hopper(self, user_id: int):
        """Track clan hopper achievement"""
        await self._retry_db_operation(self._track_clan_hopper_internal, user_id)
    
    async def _track_clan_hopper_internal(self, cursor, user_id: int):
        self._ensure_user_stats_row(cursor, user_id)
        cursor.execute('SELECT clan_transfer_count FROM user_stats WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        clan_transfer_count = (result[0] or 0) + 1 if result else 1
        
        cursor.execute('UPDATE user_stats SET clan_transfer_count = ? WHERE user_id = ?', (clan_transfer_count, user_id))
        
        if clan_transfer_count >= 1:
            await self.award_achievement(user_id, "clan_hopper", cursor=cursor)
    
    async def _track_active_channels_internal(
        self,
        cursor,
        user_id: int,
        channel_id: int,
        message_time: Optional[datetime] = None,
    ):
        self._ensure_user_stats_row(cursor, user_id)
        cursor.execute('SELECT active_channels FROM user_stats WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        active_channels = set((result[0] or '').split()) - {''} if result else set()
        
        channel_key = str(channel_id)
        if channel_key in active_channels:
            return

        message_dt = (message_time or datetime.now(timezone.utc)).astimezone(timezone.utc)
        day_key = int(message_dt.strftime("%Y%m%d"))
        daily_set = self._active_channel_daily_sets.get((user_id, day_key), set())
        if len(daily_set) >= ACTIVE_CHANNEL_DAILY_LIMIT:
            return

        daily_set.add(channel_key)
        self._active_channel_daily_sets[(user_id, day_key)] = daily_set
        active_channels.add(channel_key)
        
        cursor.execute('UPDATE user_stats SET active_channels = ? WHERE user_id = ?', (' '.join(sorted(list(active_channels))), user_id))
        
        if len(active_channels) >= 5:
            await self.award_achievement(user_id, "social_butterfly", cursor=cursor)
        if len(active_channels) >= 8:
            await self.award_achievement(user_id, "channel_explorer", cursor=cursor)
    
    def _resolve_member_timezone(self, member: Optional[discord.Member]):
        """Resolve a member's timezone from region roles; fall back to UTC."""
        if member is None:
            return timezone.utc

        for role in getattr(member, "roles", []):
            tz_name = REGION_ROLE_TIMEZONES.get(role.id)
            if not tz_name:
                continue
            resolved = resolve_timezone(tz_name)
            if resolved is not None:
                return resolved
            self.logger.error(
                "Configured timezone not found: role_id=%s tz=%s",
                role.id,
                tz_name,
            )
            return timezone.utc

        return timezone.utc

    async def _track_consistency_achievements_internal(
        self,
        cursor,
        user_id: int,
        message_time: datetime,
        member: Optional[discord.Member] = None,
    ):
        cursor.execute('''
            INSERT OR IGNORE INTO user_stats (
                user_id, last_activity_date, activity_streak, weekly_activity_count, monthly_activity_count,
                early_bird_count, night_owl_count, early_bird_last_local_day, night_owl_last_local_day
            )
            VALUES (?, 0, 0, 0, 0, 0, 0, 0, 0)
        ''', (user_id,))

        cursor.execute('''
            SELECT
                last_activity_date, activity_streak, weekly_activity_count, monthly_activity_count,
                early_bird_count, night_owl_count, early_bird_last_local_day, night_owl_last_local_day
            FROM user_stats
            WHERE user_id = ?
        ''', (user_id,))
        result = cursor.fetchone()
        (
            last_activity_date,
            activity_streak,
            weekly_activity_count,
            monthly_activity_count,
            early_bird_count,
            night_owl_count,
            early_bird_last_local_day,
            night_owl_last_local_day,
        ) = result
    
        message_dt_utc = (
            message_time.astimezone(timezone.utc)
            if message_time.tzinfo
            else message_time.replace(tzinfo=timezone.utc)
        )
        last_activity_dt = datetime.fromtimestamp(last_activity_date, tz=timezone.utc) if last_activity_date else None
    
        today_utc = message_dt_utc.date()
        yesterday_utc = today_utc - timedelta(days=1)
    
        updated_streak = activity_streak
        updated_weekly_count = weekly_activity_count
        updated_monthly_count = monthly_activity_count
    
        if not (last_activity_dt and last_activity_dt.date() == today_utc):
            if last_activity_dt and last_activity_dt.date() == yesterday_utc:
                updated_streak += 1
            else:
                updated_streak = 1

            updated_weekly_count = min(updated_streak, 28)
            updated_monthly_count = min(updated_streak, 90)
    
            cursor.execute('''
                UPDATE user_stats 
                SET last_activity_date = ?, activity_streak = ?, weekly_activity_count = ?, monthly_activity_count = ?
                WHERE user_id = ?
            ''', (int(message_dt_utc.timestamp()), updated_streak, updated_weekly_count, updated_monthly_count, user_id))
    
            if updated_streak >= 7:
                await self.award_achievement(user_id, "daily_streaker", cursor=cursor)
            if updated_weekly_count >= 28:
                await self.award_achievement(user_id, "weekly_warrior", cursor=cursor)
            if updated_monthly_count >= 90:
                await self.award_achievement(user_id, "monthly_master", cursor=cursor)

        member_tz = self._resolve_member_timezone(member)
        local_dt = message_dt_utc.astimezone(member_tz)
        local_day_key = int(local_dt.strftime("%Y%m%d"))
        local_hour = local_dt.hour

        updated_early_bird_count = early_bird_count
        updated_night_owl_count = night_owl_count
        updated_early_day = early_bird_last_local_day or 0
        updated_night_day = night_owl_last_local_day or 0

        if 4 <= local_hour < 10 and updated_early_day != local_day_key:
            updated_early_bird_count += 1
            updated_early_day = local_day_key
            if updated_early_bird_count >= 10:
                await self.award_achievement(user_id, "early_bird", cursor=cursor)

        if (local_hour >= 22 or local_hour < 4) and updated_night_day != local_day_key:
            updated_night_owl_count += 1
            updated_night_day = local_day_key
            if updated_night_owl_count >= 10:
                await self.award_achievement(user_id, "night_owl", cursor=cursor)

        if (
            updated_early_bird_count != early_bird_count
            or updated_night_owl_count != night_owl_count
            or updated_early_day != (early_bird_last_local_day or 0)
            or updated_night_day != (night_owl_last_local_day or 0)
        ):
            cursor.execute('''
                UPDATE user_stats 
                SET early_bird_count = ?, night_owl_count = ?, early_bird_last_local_day = ?, night_owl_last_local_day = ?
                WHERE user_id = ?
            ''', (updated_early_bird_count, updated_night_owl_count, updated_early_day, updated_night_day, user_id))
