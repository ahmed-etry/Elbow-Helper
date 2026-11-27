"""Achievement progress calculation and overview payload builders."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from datetime import timezone

import discord
from elbow_helper.infrastructure.discord.pagination import format_page_footer
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX

from .config import ACHIEVEMENTS_THUMBNAIL
from .definitions import COIN_REWARDS
from .queries import (
    achievement_leaderboard_eligible, achievement_progress_value,
)

class AchievementProgressMixin:
    async def _achievements_overview_internal(self, cursor, member: discord.Member):
        """Build overview payload combining completed and in-progress achievements."""
        try:
            cursor.execute(
                '''
                SELECT id, name, description, required_count, emoji
                FROM achievements
                ORDER BY name
                '''
            )
            all_achievements = cursor.fetchall()

            if not all_achievements:
                return {"error": "No achievements are available.", "embed": None, "view": None}

            cursor.execute(
                '''
                SELECT achievement_id, completed_date
                FROM user_achievements
                WHERE user_id = ? AND completed_date IS NOT NULL
                ''',
                (member.id,),
            )
            completed_map = {row[0]: row[1] for row in cursor.fetchall()}

            cursor.execute(
                '''
                SELECT message_count, emoji_count, reaction_count, voice_hours,
                       silent_voice_seconds, role_pings, meme_posts, clan_transfer_count,
                       active_channels, activity_streak, weekly_activity_count,
                       monthly_activity_count, early_bird_count, night_owl_count
                FROM user_stats WHERE user_id = ?
                ''',
                (member.id,),
            )
            user_stats = cursor.fetchone()
            if not user_stats:
                cursor.execute(
                    '''
                    INSERT OR IGNORE INTO user_stats (user_id)
                    VALUES (?)
                    ''',
                    (member.id,),
                )
                user_stats = (0, 0, 0, 0.0, 0, 0, 0, 0, "", 0, 0, 0, 0, 0)

            total_achievements = len(all_achievements)
            completed_count = len(completed_map)
            percent = round(completed_count / total_achievements * 100)

            in_progress_raw = []
            completed_raw = []
            for achievement_id, name, description, required_count, emoji in all_achievements:
                progress_info = self.calculate_achievement_progress(
                    achievement_id, user_stats, completed_map.keys(), member
                )
                required = required_count if required_count is not None else max(progress_info.get("required", 1), 1)
                current = progress_info.get("current", 0)

                reward = COIN_REWARDS.get(achievement_id)
                field_name = f"{emoji} {name}"
                if reward:
                    field_name += f" · 🪙 {reward}"
                if description.endswith("."):
                    description = description[:-1]

                if progress_info.get("completed"):
                    completed_on = completed_map.get(achievement_id)
                    completed_raw.append((completed_on or 0, field_name, description))
                else:
                    ratio = current / max(required, 1)
                    in_progress_raw.append((ratio, field_name, description, current, required))

            in_progress_raw.sort(key=lambda x: x[0], reverse=True)
            completed_raw.sort(key=lambda x: x[0])

            in_progress_fields = []
            for _, field_name, description, current, required in in_progress_raw:
                if required == 1:
                    value = description
                else:
                    bar = self.create_progress_bar(current, required)
                    value = f"{description}\n{bar} {int(current)}/{required}"
                in_progress_fields.append((field_name, value))

            completed_fields = []
            for completed_on, field_name, description in completed_raw:
                status = f"✅ <t:{completed_on}:D>" if completed_on else "✅ Completed"
                completed_fields.append((field_name, f"{description}\n{status}"))

            page_size = 6
            if not completed_fields:
                completed_fields.append(("\u200b", "No achievements completed yet."))
            if not in_progress_fields:
                in_progress_fields.append(("\u200b", "🎉 All achievements completed!"))

            picture = None
            picture_path = self.bot.paths.avatar_directory / ACHIEVEMENTS_THUMBNAIL
            if picture_path.is_file():
                try:
                    picture = discord.File(picture_path, filename=ACHIEVEMENTS_THUMBNAIL)
                except OSError as exc:
                    self.logger.warning("Achievements thumbnail %s not attached: %s", ACHIEVEMENTS_THUMBNAIL, exc)

            def build_pages(fields):
                section_pages = []
                for idx in range(0, len(fields), page_size):
                    embed = discord.Embed(
                        title="🏆 Achievements",
                        description=(
                            f"🏅 **{completed_count}** of {total_achievements} unlocked\n"
                            f"{self.create_progress_bar(completed_count, total_achievements)} {percent}%\n\u200b"
                        ),
                        color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
                    )
                    embed.set_author(
                        name=member.nick or member.display_name,
                        icon_url=member.display_avatar.with_size(64).url,
                    )
                    for field_name, value in fields[idx:idx + page_size]:
                        embed.add_field(name=field_name, value=value, inline=False)
                    if picture is not None:
                        embed.set_thumbnail(url=f"attachment://{ACHIEVEMENTS_THUMBNAIL}")
                    section_pages.append(embed)
                return section_pages

            in_progress_pages = build_pages(in_progress_fields)
            completed_pages = build_pages(completed_fields)

            first_embed = (in_progress_pages or completed_pages)[0]
            initial_section = "In Progress" if in_progress_pages else "Completed"
            initial_total = len(in_progress_pages) if in_progress_pages else len(completed_pages)
            first_embed.set_footer(text=format_page_footer(1, initial_total, section=initial_section))

            return {"error": None, "embed": first_embed, "picture": picture,
                    "in_progress_pages": in_progress_pages, "completed_pages": completed_pages}

        except sqlite3.Error as e:
            self.logger.error("Failed to load achievements overview: %s", e, exc_info=True)
            return {"error": True, "embed": None, "view": None}
    

    async def _achievements_leaderboard_internal(self, cursor, interaction: discord.Interaction, include_leadership: bool, title: str):
        cursor.execute(
            """
            SELECT ua.user_id, COUNT(*) AS achievement_count
            FROM user_achievements ua
            WHERE ua.completed_date IS NOT NULL
            GROUP BY ua.user_id
            ORDER BY achievement_count DESC, MAX(ua.completed_date) ASC, ua.user_id ASC
            """
        )
        rows = cursor.fetchall()
        cursor.execute("SELECT COUNT(*) FROM achievements")
        total = cursor.fetchone()[0]

        guild = interaction.guild
        ranking = []
        for user_id, count in rows:
            member = guild.get_member(user_id)
            if not member:
                continue
            if not achievement_leaderboard_eligible(
                {role.id for role in member.roles},
                include_leadership=include_leadership,
            ):
                continue
            ranking.append((user_id, count))

        embed = discord.Embed(
            title=title,
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX)
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        if not ranking:
            embed.description = "No members have earned any achievements yet."
            return embed

        medals = ["🥇", "🥈", "🥉"]
        blocks = []
        for index, (user_id, count) in enumerate(ranking[:3]):
            blocks.append(
                f"{medals[index]} <@{user_id}>\n"
                f"{self.create_progress_bar(count, total)} **{count}** / {total}"
            )
        if len(ranking) >= 4:
            lines = [
                f"`{('#' + str(rank)).ljust(3)}` <@{user_id}> · {count}"
                for rank, (user_id, count) in enumerate(ranking[3:10], 4)
            ]
            blocks.append("\n".join(lines))
        embed.description = "\n\n".join(blocks)

        viewer_rank = next(
            (rank for rank, (user_id, _) in enumerate(ranking, 1) if user_id == interaction.user.id),
            None,
        )
        if viewer_rank is not None:
            viewer_count = ranking[viewer_rank - 1][1]
            noun = "achievement" if viewer_count == 1 else "achievements"
            embed.set_footer(text=f"You're #{viewer_rank} with {viewer_count} {noun}")

        return embed


    def calculate_achievement_progress(self, achievement_id: str, user_stats, completed_achievements, member):
        """Calculate progress for a specific achievement"""
        try:
            if achievement_id in completed_achievements:
                return {"completed": True, "current": 0, "required": 0}
            
            details = self.get_achievement_details(achievement_id)
            if not details:
                self.logger.warning(
                    "Achievement details missing for %s; defaulting required_count=1",
                    achievement_id,
                )
                required_count = 1
            else:
                required_count = details[3] if details[3] is not None else 1

            current, _ = achievement_progress_value(
                achievement_id, tuple(user_stats),
                joined_at=member.joined_at,
                observed_at=datetime.now(timezone.utc),
            )
            return {
                "completed": False, "current": current,
                "required": required_count,
            }
                
        except (TypeError, ValueError, AttributeError, KeyError, IndexError, ZeroDivisionError) as e:
            self.logger.error(
                "Progress calculation failed for achievement_id=%s: %s",
                achievement_id,
                e,
                exc_info=True,
            )
            return {"completed": False, "current": 0, "required": 1}
    
    def create_progress_bar(self, current: int, required: int, length: int = 10):
        """Create a visual progress bar"""
        if required <= 0:
            return "▰" * length
        
        progress = min(current / required, 1.0)
        filled = int(progress * length)
        empty = length - filled
        
        return "▰" * filled + "▱" * empty
    
