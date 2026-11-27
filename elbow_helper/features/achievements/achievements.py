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
from elbow_helper.infrastructure.discord.application_emojis import get_application_emoji_provider

from .config import UNLOCK_THUMBNAILS
from .definitions import ACHIEVEMENT_PHRASES
from .definitions import COIN_REWARDS
from .definitions import description_lines

class AchievementServiceMixin:
    async def achievement_change_state(self, user_id: int, query: str):
        """Resolve an achievement and read the member's award and coin state."""
        return await self._retry_db_operation(
            self._achievement_change_state_internal, user_id, query,
        )

    async def _achievement_change_state_internal(
        self, cursor: sqlite3.Cursor, user_id: int, query: str,
    ):
        cursor.execute("SELECT id, name FROM achievements ORDER BY name")
        rows = cursor.fetchall()
        needle = query.strip().casefold()
        exact = [row for row in rows if needle in (row[0].casefold(), row[1].casefold())]
        matches = exact or [row for row in rows if needle in row[0].casefold()
                            or needle in row[1].casefold()]
        if not needle or not matches:
            return None
        if len(matches) != 1:
            raise ValueError("ambiguous achievement")
        achievement_id, name = matches[0]
        cursor.execute(
            "SELECT completed_date FROM user_achievements "
            "WHERE user_id = ? AND achievement_id = ?",
            (user_id, achievement_id),
        )
        completed = cursor.fetchone()
        cursor.execute(
            "SELECT type, COUNT(*) FROM coin_transactions "
            "WHERE user_id = ? AND reason = ? "
            "AND type IN ('achievement', 'achievement_reversal') GROUP BY type",
            (user_id, achievement_id),
        )
        counts = dict(cursor.fetchall())
        return {
            "id": achievement_id, "name": name,
            "completed_date": completed[0] if completed else None,
            "reward": COIN_REWARDS.get(achievement_id, 0),
            "reversal_due": counts.get("achievement", 0) > counts.get(
                "achievement_reversal", 0,
            ),
        }

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
                achievement_id,
                name,
                description,
                emoji,
            ):
                await self.send_achievement_notification(
                    user_id, achievement_id, name, description, emoji,
                )

    async def _achievement_counts_internal(self, cursor: sqlite3.Cursor, user_id: int):
        """Return how many achievements the member has completed and how many exist."""
        cursor.execute(
            "SELECT COUNT(*) FROM user_achievements "
            "WHERE user_id = ? AND completed_date IS NOT NULL",
            (user_id,),
        )
        completed = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM achievements")
        return completed, cursor.fetchone()[0]

    def _pick_unlock_thumbnail(self) -> str | None:
        """Return the next unlock picture in rotation, or None when no picture exists.

        Each picture is shown once before any repeats, and a new round never starts
        with the picture shown last.
        """
        directory = self.bot.paths.avatar_directory
        available = [name for name in UNLOCK_THUMBNAILS if (directory / name).is_file()]
        if not available:
            return None
        left = [name for name in self._unlock_thumbnails_left if name in available]
        if not left:
            left = list(available)
            random.shuffle(left)
            if len(left) > 1 and left[0] == self._last_unlock_thumbnail:
                left.append(left.pop(0))
        picked = left.pop(0)
        self._unlock_thumbnails_left = left
        self._last_unlock_thumbnail = picked
        return picked

    async def send_achievement_notification(
        self, user_id: int, achievement_id: str, name: str, description: str, emoji: str,
    ):
        """Send achievement notification"""
        guild = self.bot.get_guild(self.GUILD_ID)
        member = guild.get_member(user_id)
        
        if not member:
            return
        
        phrase = random.choice(ACHIEVEMENT_PHRASES)

        notification_text = f"<@{user_id}> {phrase} **{name}**!"

        embed = discord.Embed(
            title="Achievement Unlocked!",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX)
        )
        embed.set_author(
            name=member.nick or member.display_name,
            icon_url=member.display_avatar.with_size(64).url,
        )
        thumbnail_url = DEFAULT_THUMBNAIL_URL
        picture = None
        filename = self._pick_unlock_thumbnail()
        if filename:
            try:
                picture = discord.File(self.bot.paths.avatar_directory / filename, filename=filename)
            except OSError as exc:
                self.logger.warning("Unlock thumbnail %s not attached: %s", filename, exc)
            else:
                thumbnail_url = f"attachment://{filename}"
        embed.set_thumbnail(url=thumbnail_url)
        lines = description_lines(description)
        blank = None
        if callable(getattr(self.bot, "fetch_application_emojis", None)):
            catalog = await get_application_emoji_provider(self.bot).get(required_names=("blank",))
            blank = catalog.get("blank")
        new_line = f"\n{blank} " if blank else "\n"
        value = f"{emoji} {new_line.join(lines)}"
        embed.add_field(name=name, value=f"{value}\n\u200b", inline=False)

        # Leadership earns no coins.
        reward = 0 if self._is_leadership_any(member) else COIN_REWARDS.get(achievement_id, 0)
        if reward:
            coins = "coin" if reward == 1 else "coins"
            embed.add_field(name="Reward", value=f"🪙 {reward} {coins}", inline=False)

        try:
            completed, total = await self._retry_db_operation(
                self._achievement_counts_internal, user_id,
            )
        except sqlite3.Error:
            self.logger.exception("Failed to count achievements for user_id=%s", user_id)
        else:
            embed.add_field(
                name="Progress", value=f"🏆 {completed} of {total} achievements", inline=False,
            )

        channel = self.bot.get_channel(GENERAL_CHAT)
        
        if channel:
            await channel.send(
                content=notification_text,
                embed=embed,
                file=picture,
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

