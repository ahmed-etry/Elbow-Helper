"""Achievement progress calculation and overview payload builders."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from datetime import timezone

import discord
from elbow_helper.discord.pagination import format_page_footer
from elbow_helper.configuration.roles import LEAD
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX
from elbow_helper.configuration.style import DEFAULT_THUMBNAIL_URL

from .definitions import COIN_REWARDS
from .views import AchievementOverviewView

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
            completion_rate = (completed_count / total_achievements * 100) if total_achievements else 0

            completed_entries = []
            in_progress_entries = []
            in_progress_raw = []
            completed_raw = []
            for achievement_id, name, description, required_count, emoji in all_achievements:
                progress_info = self.calculate_achievement_progress(
                    achievement_id, user_stats, completed_map.keys(), member
                )
                required = required_count if required_count is not None else max(progress_info.get("required", 1), 1)
                current = progress_info.get("current", 0)

                reward = COIN_REWARDS.get(achievement_id)
                reward_text = f" `(+{reward}🪙)`" if reward else ""

                if progress_info.get("completed"):
                    completed_on = completed_map.get(achievement_id)
                    completed_raw.append((completed_on or 0, emoji, name, reward_text, description))
                else:
                    ratio = current / max(required, 1)
                    progress_bar = self.create_progress_bar(current, required)
                    in_progress_raw.append((ratio, emoji, name, reward_text, progress_bar, current, required, description))

            in_progress_raw.sort(key=lambda x: x[0], reverse=True)
            completed_raw.sort(key=lambda x: x[0])

            for _, emoji, name, reward_text, progress_bar, current, required, description in in_progress_raw:
                entry = (
                    f"{emoji} **{name}**{reward_text}\n"
                    f"{progress_bar} {current}/{required}\n"
                    f"{description}"
                )
                in_progress_entries.append(entry)

            for completed_on, emoji, name, reward_text, description in completed_raw:
                date_txt = (
                    datetime.fromtimestamp(completed_on, tz=timezone.utc).strftime("%b %d, %Y")
                    if completed_on
                    else "Completed"
                )
                entry = (
                    f"{emoji} **{name}**{reward_text}\n"
                    f"{description}\n"
                    f"_Completed: {date_txt}_"
                )
                completed_entries.append(entry)

            page_size = 6
            if not completed_entries:
                completed_entries.append("No achievements completed yet.")
            if not in_progress_entries:
                in_progress_entries.append("🎉 All achievements completed!")

            def build_pages(items, section_title):
                section_pages = []
                for idx in range(0, len(items), page_size):
                    chunk = items[idx:idx + page_size]
                    embed = discord.Embed(
                        title=f"🏆 Achievements: {member.display_name}",
                        description=(
                            f"Completed **{completed_count}/{total_achievements}** "
                            f"({completion_rate:.1f}%).\n{section_title}"
                        ),
                        color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
                    )
                    embed.add_field(
                        name="\u200b",
                        value="\n\n".join(chunk),
                        inline=False,
                    )
                    embed.set_thumbnail(
                        url=DEFAULT_THUMBNAIL_URL
                    )
                    section_pages.append(embed)
                return section_pages

            in_progress_pages = build_pages(in_progress_entries, "")
            completed_pages = build_pages(completed_entries, "")

            view = AchievementOverviewView(in_progress_pages, completed_pages) if (len(in_progress_pages) + len(completed_pages)) > 1 else None
            first_embed = (in_progress_pages or completed_pages)[0]
            initial_section = "In Progress" if in_progress_pages else "Completed"
            initial_total = len(in_progress_pages) if in_progress_pages else len(completed_pages)
            first_embed.set_footer(text=format_page_footer(1, initial_total, section=initial_section))

            return {"error": None, "embed": first_embed, "view": view}

        except sqlite3.Error as e:
            self.logger.error("Failed to load achievements overview: %s", e, exc_info=True)
            return {"error": True, "embed": None, "view": None}
    

    async def _achievements_leaderboard_internal(self, cursor, interaction: discord.Interaction, include_leadership: bool, title: str):
        sql = """
            SELECT ua.user_id, COUNT(*) as achievement_count
            FROM user_achievements ua
            WHERE ua.completed_date IS NOT NULL
        """
        sql += " GROUP BY ua.user_id ORDER BY achievement_count DESC"
        cursor.execute(sql)
        rows = cursor.fetchall()

        guild = interaction.guild
        leaderboard = []
        for user_id, count in rows:
            member = guild.get_member(user_id)
            if not member:
                continue
            has_leader = any(r.id in LEAD for r in member.roles)
            if not include_leadership and has_leader:
                continue
            leaderboard.append((member, count))
            if len(leaderboard) >= 10:
                break

        embed = discord.Embed(
            title=title,
            description="Top achievement earners",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX)
        )

        for i, (member, count) in enumerate(leaderboard, 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            embed.add_field(
                name=f"{medal} {member.display_name}",
                value="1 achievement" if count == 1 else f"{count} achievements",
                inline=False
            )

        if not leaderboard:
            embed.description = "No members have earned any achievements yet."

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

            (message_count, emoji_count, reaction_count, voice_hours, silent_voice_seconds, role_pings,
             meme_posts, clan_transfer_count, active_channels, activity_streak,
             weekly_activity_count, monthly_activity_count, early_bird_count, night_owl_count) = user_stats
            
            if achievement_id == "chatterbox":
                return {"completed": False, "current": message_count, "required": required_count}
            elif achievement_id == "keyboard_warrior":
                return {"completed": False, "current": message_count, "required": required_count}
            elif achievement_id == "emoji_enthusiast":
                return {"completed": False, "current": emoji_count, "required": required_count}
            elif achievement_id == "react_lord":
                return {"completed": False, "current": reaction_count, "required": required_count}
            elif achievement_id == "ping_collector":
                return {"completed": False, "current": role_pings, "required": required_count}
            elif achievement_id == "meme_dealer":
                return {"completed": False, "current": meme_posts, "required": required_count}
            elif achievement_id == "social_butterfly":
                channel_count = len(active_channels.split()) if active_channels else 0
                return {"completed": False, "current": channel_count, "required": required_count}
            elif achievement_id == "channel_explorer":
                channel_count = len(active_channels.split()) if active_channels else 0
                return {"completed": False, "current": channel_count, "required": required_count}
            elif achievement_id == "daily_streaker":
                return {"completed": False, "current": activity_streak, "required": required_count}
            elif achievement_id == "weekly_warrior":
                return {"completed": False, "current": weekly_activity_count, "required": required_count}
            elif achievement_id == "monthly_master":
                return {"completed": False, "current": monthly_activity_count, "required": required_count}
            elif achievement_id == "early_bird":
                return {"completed": False, "current": early_bird_count, "required": required_count}
            elif achievement_id == "night_owl":
                return {"completed": False, "current": night_owl_count, "required": required_count}
            elif achievement_id == "marathoner":
                return {"completed": False, "current": int(voice_hours), "required": required_count}
            elif achievement_id == "silent_lurker":
                return {
                    "completed": False,
                    "current": round(float(silent_voice_seconds) / 3600.0, 2),
                    "required": required_count,
                }
            elif achievement_id == "clan_hopper":
                return {"completed": False, "current": clan_transfer_count, "required": required_count}
            elif achievement_id == "storyteller":
                return {"completed": False, "current": 0, "required": 1}
            elif achievement_id == "mic_check":
                if achievement_id in completed_achievements:
                    return {"completed": True, "current": 1, "required": 1}
                else:
                    return {"completed": False, "current": 0, "required": 1}
            elif achievement_id == "party_animal":
                return {"completed": False, "current": 0, "required": 1}
            elif achievement_id == "fresh_recruit":
                return {"completed": False, "current": 0, "required": 1}
            elif achievement_id == "promoted":
                return {"completed": False, "current": 0, "required": 1}
            elif achievement_id == "hibernation_survivor":
                return {"completed": False, "current": 0, "required": 1}
            elif achievement_id == "random_crit":
                return {"completed": False, "current": 0, "required": 1}
            elif achievement_id == "one_of_us":
                if member.joined_at:
                    days_in_server = (datetime.now(timezone.utc) - member.joined_at).days
                    return {"completed": False, "current": days_in_server, "required": required_count}
                else:
                    return {"completed": False, "current": 0, "required": required_count}
            elif achievement_id == "veteran":
                if member.joined_at:
                    days_in_server = (datetime.now(timezone.utc) - member.joined_at).days
                    return {"completed": False, "current": days_in_server, "required": required_count}
                else:
                    return {"completed": False, "current": 0, "required": required_count}
            else:
                return {"completed": False, "current": 0, "required": 1}
                
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
    
