"""Achievement award, removal, notification, and helper routines."""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime
from datetime import timezone

import discord
from elbow_helper.configuration.channels import GENERAL_CHAT
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX
from elbow_helper.configuration.style import DEFAULT_THUMBNAIL_URL

from .definitions import ACHIEVEMENT_PHRASES
from .definitions import COIN_REWARDS

class AchievementServiceMixin:
    async def award_achievement(self, user_id: int, achievement_id: str, announce: bool = True, cursor: sqlite3.Cursor = None):
        """Award an achievement to a user"""
        if cursor:
            await self._award_achievement_internal(cursor, user_id, achievement_id, announce)
        else:
            await self._retry_db_operation(self._award_achievement_internal, user_id, achievement_id, announce)
    
    async def _award_achievement_internal(self, cursor: sqlite3.Cursor, user_id: int, achievement_id: str, announce: bool = True):
        cursor.execute('''
            SELECT completed_date FROM user_achievements 
            WHERE user_id = ? AND achievement_id = ?
        ''', (user_id, achievement_id))
        if cursor.fetchone():
            return
        
        cursor.execute(
            '''
            SELECT id, name, description, required_count, emoji
            FROM achievements
            WHERE id = ?
            ''',
            (achievement_id,),
        )
        achievement_details = cursor.fetchone()
        if not achievement_details:
            self.logger.error(
                "Cannot award achievement: achievement_id=%s user_id=%s reason=not_found",
                achievement_id,
                user_id,
            )
            return
        
        _, name, description, _, emoji = achievement_details
        
        cursor.execute('''
            INSERT INTO user_achievements (user_id, achievement_id, completed_date)
            VALUES (?, ?, ?)
        ''', (user_id, achievement_id, int(datetime.now(timezone.utc).timestamp())))
        reward = COIN_REWARDS.get(achievement_id, 0)
        if reward:
            await self._add_coins(cursor, user_id, reward, 'achievement', achievement_id, None)
        
        if announce:
            if not self._queue_post_commit_action(
                self.send_achievement_notification,
                user_id,
                name,
                description,
                emoji,
            ):
                await self.send_achievement_notification(user_id, name, description, emoji)
    
    async def send_achievement_notification(self, user_id: int, name: str, description: str, emoji: str):
        """Send achievement notification"""
        guild = self.bot.get_guild(self.GUILD_ID)
        member = guild.get_member(user_id)
        
        if not member:
            return
        
        phrase = random.choice(ACHIEVEMENT_PHRASES)
        
        colored_name = f"**{name}**"
        
        notification_text = f"<@{user_id}> {phrase} {colored_name}!"
        
        embed = discord.Embed(
            title=f"{emoji} Achievement Unlocked!",
            description=f"**{name}**\n\n{description}",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX)
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        embed.add_field(name="👤 Member", value=member.display_name, inline=True)
        embed.add_field(name="📅 Date", value=datetime.fromtimestamp(int(datetime.now(timezone.utc).timestamp()), tz=timezone.utc).strftime("%B %d, %Y"), inline=True)
        
        channel = self.bot.get_channel(GENERAL_CHAT)
        
        if channel:
            await channel.send(
                content=notification_text,
                embed=embed
            )
    

    async def manually_award_achievement(self, user_id: int, achievement_id: str, awarded_by: str, silent: bool = False):
        """Helper method to manually award an achievement"""
        try:
            return await self._retry_db_operation(
                self._manually_award_achievement_internal,
                user_id,
                achievement_id,
                awarded_by,
                silent,
            )
        except (sqlite3.Error, discord.HTTPException) as e:
            self.logger.error("Manual achievement award failed: %s", e, exc_info=True)
            return False, "Couldn't award that achievement right now. Try again in a moment."

    async def _manually_award_achievement_internal(
        self,
        cursor: sqlite3.Cursor,
        user_id: int,
        achievement_id: str,
        awarded_by: str,
        silent: bool = False,
    ):
        cursor.execute(
            '''
            SELECT id, name, description, required_count, emoji
            FROM achievements
            WHERE id = ?
            ''',
            (achievement_id,),
        )
        achievement_details = cursor.fetchone()
        if not achievement_details:
            return False, f"Achievement '{achievement_id}' not found"

        cursor.execute(
            '''
            SELECT completed_date FROM user_achievements
            WHERE user_id = ? AND achievement_id = ?
            ''',
            (user_id, achievement_id),
        )
        if cursor.fetchone():
            return False, f"User already has the '{achievement_details[1]}' achievement"

        await self._award_achievement_internal(cursor, user_id, achievement_id, announce=not silent)
        self.logger.info(
            "Manual achievement award: actor=%s user_id=%s achievement_id=%s silent=%s",
            awarded_by,
            user_id,
            achievement_id,
            silent,
        )
        return True, f"Awarded **{achievement_details[1]}** \u2705"

    async def manually_remove_achievement(self, user_id: int, achievement_id: str, removed_by: str):
        """Helper method to manually remove an achievement"""
        try:
            return await self._retry_db_operation(
                self._manually_remove_achievement_internal,
                user_id,
                achievement_id,
                removed_by,
            )
        except sqlite3.Error as e:
            self.logger.error("Manual achievement removal failed: %s", e, exc_info=True)
            return False, "Couldn't remove that achievement right now. Try again in a moment."

    async def _manually_remove_achievement_internal(
        self,
        cursor: sqlite3.Cursor,
        user_id: int,
        achievement_id: str,
        removed_by: str,
    ):
        cursor.execute(
            '''
            SELECT id, name
            FROM achievements
            WHERE id = ?
            ''',
            (achievement_id,),
        )
        achievement_details = cursor.fetchone()
        if not achievement_details:
            return False, f"Achievement '{achievement_id}' not found"

        cursor.execute(
            '''
            SELECT 1
            FROM user_achievements
            WHERE user_id = ? AND achievement_id = ?
            ''',
            (user_id, achievement_id),
        )
        if not cursor.fetchone():
            return False, f"User doesn't have the '{achievement_details[1]}' achievement"

        cursor.execute(
            '''
            DELETE FROM user_achievements
            WHERE user_id = ? AND achievement_id = ?
            ''',
            (user_id, achievement_id),
        )

        reward = COIN_REWARDS.get(achievement_id, 0)
        if reward > 0:
            await self._ensure_coin_row(cursor, user_id)
            # Keep removals idempotent: only reverse coins if awards outnumber prior reversals.
            cursor.execute(
                '''
                SELECT COUNT(*)
                FROM coin_transactions
                WHERE user_id = ? AND type = 'achievement' AND reason = ?
                ''',
                (user_id, achievement_id),
            )
            award_count = int(cursor.fetchone()[0] or 0)
            cursor.execute(
                '''
                SELECT COUNT(*)
                FROM coin_transactions
                WHERE user_id = ? AND type = 'achievement_reversal' AND reason = ?
                ''',
                (user_id, achievement_id),
            )
            reversal_count = int(cursor.fetchone()[0] or 0)
            if award_count > reversal_count:
                await self._add_coins(
                    cursor,
                    user_id,
                    -reward,
                    'achievement_reversal',
                    achievement_id,
                    None,
                )

        self.logger.info(
            "Manual achievement removal: actor=%s user_id=%s achievement_id=%s",
            removed_by,
            user_id,
            achievement_id,
        )
        return True, f"Removed **{achievement_details[1]}** \u2705"

