"""Slash command surface for achievements, economy, and raffle."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from datetime import timezone
from typing import Literal
from typing import Optional

import discord
from discord import app_commands
from elbow_helper.discord.interactions import deny
from elbow_helper.discord.interactions import fail
from elbow_helper.discord.interactions import warn
from elbow_helper.configuration.channels import GIVEAWAYS
from elbow_helper.configuration.roles import CORE
from elbow_helper.configuration.roles import LEAD
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX
from elbow_helper.configuration.style import DEFAULT_THUMBNAIL_URL

from .config import DAILY_MIN_CHARS
from .config import DAILY_MSG_THRESHOLD
from .config import DAILY_REWARD
from .config import DAILY_REWARD_ELDER
from .config import MANUAL_CAP_CWL
from .config import MANUAL_CAP_ENCOURAGEMENT
from .config import MANUAL_CAP_TOTAL
from .config import SALARY_AMOUNT
from .config import TICKET_COST
from .config import TICKET_LIMIT_PER_MONTH
from .definitions import COIN_REWARDS
from .views import CoinLogView

class AchievementCommandMixin:
    raffle_group = app_commands.Group(name="raffle", description="Manage raffles")
    grant_group = app_commands.Group(name="grant", description="Grant coins or tickets")
    achievement_group = app_commands.Group(name="achievement", description="Manage achievements")

    async def achievement_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """Autocomplete for achievement names"""
        try:
            if not current:
                return []
            
            conn = sqlite3.connect(self.db_path, timeout=20.0)
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, name, emoji
                FROM achievements
                WHERE (name LIKE ? OR id LIKE ?)
                ORDER BY name
                LIMIT 25
            ''', (f'%{current}%', f'%{current}%'))
            achievements = cursor.fetchall()
            conn.close()
            
            choices = []
            for achievement_id, name, emoji in achievements:
                label = f"{emoji} {name}"
                choices.append(app_commands.Choice(name=label, value=achievement_id))
            
            return choices
        except sqlite3.Error:
            self.logger.exception("Error in achievement autocomplete")
            return []

    @app_commands.command(name="achievements", description="View achievements, progress, and remaining goals")
    @app_commands.describe(member="Check someone else's achievements.")
    async def achievements(self, interaction: discord.Interaction, member: discord.Member = None):
        """Unified achievements overview showing completed and in-progress items."""
        member = member or interaction.user
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            self.logger.warning(
                "Interaction expired before /achievements defer: user_id=%s interaction_id=%s",
                getattr(interaction.user, "id", None),
                getattr(interaction, "id", None),
            )
            return
        except discord.HTTPException:
            self.logger.exception(
                "Failed to defer /achievements interaction: user_id=%s interaction_id=%s",
                getattr(interaction.user, "id", None),
                getattr(interaction, "id", None),
            )
            return
        payload = await self._retry_db_operation(self._achievements_overview_internal, member)
        if payload.get("error"):
            await fail(interaction)
            return
        message = await interaction.followup.send(
            embed=payload["embed"],
            view=payload["view"],
            ephemeral=True,
            wait=True,
        )
        if payload["view"] is not None:
            payload["view"].bind_message(message)

    @achievement_group.command(name="leaderboard", description="See which members have earned the most achievements.")
    async def leaderboard(self, interaction: discord.Interaction):
        """See which members have earned the most achievements. (members only)"""
        embed = await self._retry_db_operation(
            self._achievements_leaderboard_internal,
            interaction,
            False,
            "🏆 Member Achievement Leaderboard",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="inventory", description="Check your coins and raffle ticket.")
    @app_commands.describe(user="Leadership can choose another member. Leave empty to view your own inventory.")
    async def inventory(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        target = user or interaction.user
        if user and not self._is_leadership(interaction.user):
            await interaction.response.send_message("You can only view your own inventory.", ephemeral=True)
            return
        balance, has_ticket = await self._retry_db_operation(self._get_inventory_internal, target.id)
        embed = self._build_inventory_embed(target, balance, has_ticket)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="coinlog", description="See a member's latest coin earnings and spending.")
    @app_commands.describe(user="Member whose coin activity you want to view.")
    async def coinlog(self, interaction: discord.Interaction, user: discord.Member):
        if not self._is_core(interaction.user):
            await deny(interaction)
            return
        rows = await self._retry_db_operation(self._coinlog_internal, user.id)
        if not rows:
            await interaction.response.send_message("No coin activity found.", ephemeral=True)
            return
        embeds = self._build_coinlog_embeds(user, rows)
        view = CoinLogView(embeds)
        await interaction.response.send_message(
            embed=embeds[0],
            view=view if len(embeds) > 1 else None,
            ephemeral=True,
        )

    def _build_coinlog_embeds(self, user: discord.Member, rows) -> list[discord.Embed]:
        page_size = 10
        total_pages = max(1, (len(rows) + page_size - 1) // page_size)
        pages: list[discord.Embed] = []
        for page_index in range(total_pages):
            start = page_index * page_size
            page_rows = rows[start : start + page_size]
            blocks = [
                self._format_coinlog_entry(start + offset, *row)
                for offset, row in enumerate(page_rows, start=1)
            ]
            embed = discord.Embed(
                title=f"Coin log for {user.display_name}",
                description="\n\n".join(blocks)[:3900],
                color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            )
            embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
            embed.set_footer(
                text=f"Page {page_index + 1}/{total_pages} • Most recent first."
            )
            pages.append(embed)
        return pages

    def _format_coinlog_entry(self, index: int, amount: int, typ: str, reason, actor_id, ts: int) -> str:
        amount_text = f"+{amount:,}" if amount >= 0 else f"{amount:,}"
        kind_text = self._coinlog_type_label(typ)
        actor_text = f"<@{actor_id}>" if actor_id else "System"
        reason_text = self._coinlog_reason_text(typ, reason)
        coin_word = "coin" if abs(amount) == 1 else "coins"
        lines = [
            f"**{index}. {kind_text}**",
            f"Change: **{amount_text} {coin_word}**",
            f"By: {actor_text}",
        ]
        if reason_text:
            lines.append(f"Reason: {reason_text}")
        lines.append(f"Date: <t:{ts}:F> (<t:{ts}:R>)")
        return "\n".join(lines)

    def _coinlog_type_label(self, typ: str) -> str:
        labels = {
            "achievement": "Achievement reward",
            "daily": "Daily activity reward",
            "salary": "Monthly salary",
            "manual_cwl": "CWL bonus",
            "manual_encouragement": "Encouragement bonus",
            "raffle_purchase": "Raffle ticket purchase",
            "ticket_grant": "Raffle ticket grant",
            "ticket_remove": "Raffle ticket removal",
            "raffle_win": "Raffle win",
            "raffle_reroll": "Raffle reroll",
        }
        key = (typ or "").strip().lower()
        if key in labels:
            return labels[key]
        return key.replace("_", " ").title() if key else "Coin activity"

    def _coinlog_reason_text(self, typ: str, reason) -> str:
        if not reason:
            return ""
        text = str(reason).strip()
        key = (typ or "").strip().lower()
        if key == "daily" and text == "daily_drip":
            return "Daily activity reward"
        if key == "achievement":
            return f"Achievement: {text.replace('_', ' ').title()}"
        if key == "salary" and text.startswith("salary_") and len(text) == 13 and text[7:].isdigit():
            month_key = text[7:]
            year = int(month_key[:4])
            month = int(month_key[4:])
            month_label = datetime(year, month, 1).strftime("%B %Y")
            return f"Salary for {month_label}"
        if key in {"manual_cwl", "manual_encouragement"}:
            return text
        if key in {"ticket_grant", "ticket_remove", "raffle_purchase", "raffle_win", "raffle_reroll"}:
            return text.replace("_", " ").title()
        return text

    @raffle_group.command(name="list", description="See which members have a raffle ticket this month.")
    async def raffle_list(self, interaction: discord.Interaction):
        if not self._require_command_role(interaction):
            await deny(interaction)
            return
        rows = await self._retry_db_operation(self._raffle_list_internal)
        guild = interaction.guild
        lines = []
        for uid in rows:
            member = guild.get_member(uid) if guild else None
            lines.append(member.display_name if member else f"Unknown member ({uid})")
        desc = "\n".join(lines) or "No tickets this month."
        embed = discord.Embed(
            title="🎟️ This Month's Raffle Tickets",
            description=desc[:3900],
            color=discord.Color.gold()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @raffle_group.command(name="remove", description="Remove a member's raffle ticket for this month.")
    @app_commands.describe(user="Member whose raffle ticket you want to remove.")
    async def raffle_remove(self, interaction: discord.Interaction, user: discord.Member):
        if not any(r.id in CORE for r in getattr(interaction.user, "roles", [])):
            await deny(interaction)
            return
        await interaction.response.defer(ephemeral=True)
        msg = await self._retry_db_operation(self._raffle_remove_internal, user.id)
        await interaction.followup.send(msg, ephemeral=True)

    @grant_group.command(name="ticket", description="Give a raffle ticket to a member.")
    @app_commands.describe(
        user="Member who should receive the ticket.",
        reason="Why the member is receiving the ticket.",
    )
    async def grant_ticket(self, interaction: discord.Interaction, user: discord.Member, reason: str):
        if not self._require_command_role(interaction):
            await deny(interaction)
            return
        await interaction.response.defer(ephemeral=True)
        ok, msg = await self._retry_db_operation(self._grant_ticket_internal, user.id, reason)
        await interaction.followup.send(msg, ephemeral=True)
        if ok:
            await self.update_raffle_hub_message()

    @raffle_group.command(name="draw", description="Draw raffle winners for this month or an earlier month.")
    @app_commands.describe(month="Month to draw in YYYY-MM. Leave empty for the current month.")
    async def draw_raffle(self, interaction: discord.Interaction, month: Optional[str] = None):
        if not self._require_command_role(interaction):
            await deny(interaction)
            return
        current_month_key = self._month_key()
        target_month_key = current_month_key
        if month:
            target_month_key = self._parse_month_label(month.strip())
            if target_month_key is None:
                await warn(
                    interaction,
                    "That doesn't look like a valid month. Use the format YYYY-MM, like 2025-09.",
                )
                return
            if target_month_key > current_month_key:
                await interaction.response.send_message("You can't draw raffle winners for a future month.", ephemeral=True)
                return
        await interaction.response.defer(ephemeral=False)
        ok, winners, info = await self._retry_db_operation(
            self._draw_raffle_internal,
            interaction.guild.id,
            target_month_key,
        )
        if not ok:
            await interaction.followup.send(info, ephemeral=False)
            return
        winner_mentions = ", ".join(f"<@{uid}>" for uid in winners)
        reward = await self._retry_db_operation(self._get_meta, f"reward_{target_month_key}")
        reward_text = reward.strip() if isinstance(reward, str) and reward.strip() else "Prize not set"
        await interaction.followup.send(
            f"Congratulations, {winner_mentions}! You won this month's raffle prize: **{reward_text}**. "
            f"Contact <@327057918992187395> to collect it.",
            ephemeral=False,
        )
        await self.update_raffle_hub_message()

    @raffle_group.command(name="reroll", description="Draw this month's raffle winners again.")
    async def raffle_reroll(self, interaction: discord.Interaction):
        if not self._require_command_role(interaction):
            await deny(interaction)
            return
        await interaction.response.defer(ephemeral=False)
        ok, winners, info = await self._retry_db_operation(
            self._raffle_reroll_internal, interaction.guild.id
        )
        if not ok:
            await interaction.followup.send(info, ephemeral=False)
            return

        winner_mentions = ", ".join(f"<@{winner_id}>" for winner_id in winners)
        result_label = "New result" if len(winners) == 1 else "New results"
        embed = discord.Embed(
            title="Raffle Redrawn",
            description=f"{result_label}: {winner_mentions}",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(timezone.utc),
        )
        await interaction.followup.send(embed=embed, ephemeral=False)
        await self.update_raffle_hub_message()


    @raffle_group.command(name="history", description="See the raffle prize and winners for a selected month.")
    @app_commands.describe(month="Month to view in YYYY-MM. Leave empty for the current month.")
    async def raffle_history(self, interaction: discord.Interaction, month: Optional[str] = None):
        if not self._require_command_role(interaction):
            await deny(interaction)
            return
        await interaction.response.defer(ephemeral=True)

        if month:
            month_key = self._parse_month_label(month)
            if month_key is None:
                await warn(
                    interaction,
                    "That doesn't look like a valid month. Use the format YYYY-MM, like 2025-09.",
                )
                return
        else:
            month_key = self._month_key()

        rows, reward, winners_raw = await self._retry_db_operation(
            self._raffle_history_internal, month_key
        )
        if not rows:
            await interaction.followup.send("No raffle winners recorded for that month.", ephemeral=True)
            return

        month_label = self._month_label_from_key(month_key)
        try:
            winners_target = max(1, int(winners_raw)) if winners_raw else 1
        except (TypeError, ValueError):
            winners_target = 1
        reward_text = reward if reward else "Prize not set"

        active_lines = []
        replaced_lines = []
        for user_id, drawn_at, draw_type, _reroll_of, is_active in rows:
            label = "Draw" if draw_type == "draw" else "Reroll"
            line = f"- <@{user_id}> • {label} • <t:{int(drawn_at)}:R>"
            if is_active:
                active_lines.append(line)
            else:
                replaced_lines.append(line)

        embed = discord.Embed(
            title=f"Raffle History — {month_label}",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Prize", value=reward_text, inline=False)
        embed.add_field(name="Winners to Draw", value=str(winners_target), inline=True)
        embed.add_field(name="Current Winners", value="\n".join(active_lines) or "None", inline=False)
        if replaced_lines:
            embed.add_field(name="Earlier Draws", value="\n".join(replaced_lines), inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @raffle_group.command(name="clear", description="Clear this month's raffle winners.")
    @app_commands.describe(clear_tickets="Also clear all raffle tickets for this month.")
    async def raffle_clear(self, interaction: discord.Interaction, clear_tickets: bool = False):
        if not any(r.id in CORE for r in getattr(interaction.user, "roles", [])):
            await deny(interaction)
            return
        await interaction.response.defer(ephemeral=True)
        msg = await self._retry_db_operation(self._raffle_clear_internal, clear_tickets)
        await interaction.followup.send(msg, ephemeral=True)
        await self.update_raffle_hub_message()

    @raffle_group.command(name="prize", description="Set this month's raffle prize.")
    @app_commands.describe(
        prize="Prize to show in raffle posts.",
        winners="How many winners to draw this month.",
    )
    async def set_raffle_reward(
        self,
        interaction: discord.Interaction,
        prize: str,
        winners: app_commands.Range[int, 1] = 1,
    ):
        if not any(r.id in CORE for r in getattr(interaction.user, "roles", [])):
            await deny(interaction)
            return
        prize = prize.strip()
        if not prize:
            await interaction.response.send_message("Enter a raffle prize.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        month_key = self._month_key()
        await self._retry_db_operation(self._set_meta, f"reward_{month_key}", prize)
        await self._retry_db_operation(self._set_meta, f"winners_{month_key}", str(winners))
        if winners == 1:
            saved_message = f"Saved this month's raffle prize: {prize}"
        else:
            saved_message = f"Saved this month's raffle prize for {winners} winners: {prize}"
        await interaction.followup.send(saved_message, ephemeral=True)
        _, hub_month = await self._retry_db_operation(self._get_raffle_hub_state_internal)
        if hub_month != month_key:
            await self._retry_db_operation(self._set_raffle_hub_state_internal, "active", month_key)
            await self.update_raffle_hub_message(force_new=True)
        else:
            await self.update_raffle_hub_message()

    @app_commands.command(name="economyinfo", description="Explain how coins, tickets, and prizes work.")
    async def economy_info(self, interaction: discord.Interaction):
        month_key = self._month_key()
        reward = await self._retry_db_operation(self._get_meta, f"reward_{month_key}")
        reward_text = reward if reward else "Prize not set"
        month_label = datetime.now(timezone.utc).strftime("%B %Y")

        ach_total = sum(COIN_REWARDS.values())
        embed = discord.Embed(
            title="Coin & Raffle Info",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        embed.add_field(
            name="🎁 This Month's Prize",
            value=f"{month_label}: {reward_text}",
            inline=False,
        )
        embed.add_field(
            name="🎟️ Tickets",
            value=(
                f"A ticket costs **{TICKET_COST} coins**, and you can hold **{TICKET_LIMIT_PER_MONTH}** each month.\n"
                "Leadership can't enter the raffle.\n"
                "Tickets reset each month, so you can buy again.\n"
                "The draw happens near the end of the month. After the raffle is drawn, "
                "ticket sales stay closed for the rest of the month.\n\n"
                f"Head to <#{GIVEAWAYS}> to buy yours."
            ),
            inline=False,
        )
        embed.add_field(
            name="🪙 Coins",
            value=(
                f"- Earn up to {ach_total} coins from achievements.\n"
                f"- Send {DAILY_MSG_THRESHOLD}+ messages longer than {DAILY_MIN_CHARS} characters "
                f"in a day to earn {DAILY_REWARD} coin. Elders earn {DAILY_REWARD_ELDER} coins.\n"
                f"- Elders also receive +{SALARY_AMOUNT} once a month.\n"
                f"- Earn up to +{MANUAL_CAP_CWL} from CWL bonuses and "
                f"+{MANUAL_CAP_ENCOURAGEMENT} from encouragement bonuses each month, "
                f"up to {MANUAL_CAP_TOTAL} combined."
            ),
            inline=False,
        )
        embed.add_field(
            name="🏅 CWL Bonus Prizes",
            value=(
                "- In BEH and BE4, a bonus awards one raffle ticket.\n"
                "- In every other clan, a bonus awards members +5 coins and elders +10 coins."
            ),
            inline=False,
        )
        embed.add_field(
            name="📜 Notes",
            value=(
                "Coins don't expire. Use `/inventory` to check your balance and "
                "`/achievements` to see achievement coin rewards."
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)


    @grant_group.command(name="coins", description="Add coins to a member's balance.")
    @app_commands.describe(
        user="Member receiving the coins.",
        category="Type of coin award: CWL or encouragement.",
        amount="How many coins to give.",
        reason="Short reason that will appear in the coin log.",
    )
    async def grant_coins(self, interaction: discord.Interaction, user: discord.Member, category: Literal["cwl", "encouragement"], amount: int, reason: str):
        if not self._require_command_role(interaction):
            await deny(interaction)
            return
        amount = max(1, min(amount, 10))
        await interaction.response.defer(ephemeral=True)
        _, msg = await self._retry_db_operation(self._grant_coins_internal, user, category, amount, reason, interaction.user)
        await interaction.followup.send(msg, ephemeral=True)

    @achievement_group.command(name="award", description="Add an achievement to a member's profile.")
    @app_commands.describe(
        user="Member receiving the achievement.",
        achievement="Achievement to award.",
        silent="Skip the public announcement.",
    )
    @app_commands.autocomplete(achievement=achievement_autocomplete)
    async def award_achievement_manual(self, interaction: discord.Interaction, user: discord.Member, achievement: str, silent: bool = False):
        """Manually award an achievement to a user"""
        if not any(role.id in LEAD for role in interaction.user.roles):
            await deny(interaction)
            return
        
        try:
            if not achievement:
                await warn(interaction, "Choose an achievement to award.")
                return
            
            success, message = await self.manually_award_achievement(
                user.id, 
                achievement, 
                interaction.user.display_name,
                silent
            )
            
            if success:
                await interaction.response.send_message(f"{message} to {user.display_name}.", ephemeral=True)
            else:
                await interaction.response.send_message(
                    message or "Couldn't update that achievement right now. Try again in a moment.",
                    ephemeral=True,
                )
            
        except (sqlite3.Error, discord.HTTPException) as e:
            self.logger.error(
                "Manual award command failed: user_id=%s achievement_id=%s error=%s",
                user.id,
                achievement,
                e,
                exc_info=True,
            )
            await fail(interaction)

    @achievement_group.command(name="remove", description="Remove an achievement from a member's profile.")
    @app_commands.describe(
        user="Member whose achievement you want to remove.",
        achievement="Achievement to remove.",
    )
    @app_commands.autocomplete(achievement=achievement_autocomplete)
    async def remove_achievement_manual(self, interaction: discord.Interaction, user: discord.Member, achievement: str):
        """Manually remove an achievement from a user"""
        if not any(role.id in LEAD for role in interaction.user.roles):
            await deny(interaction)
            return
        
        try:
            if not achievement:
                await warn(interaction, "Choose an achievement to remove.")
                return
            
            success, message = await self.manually_remove_achievement(
                user.id, 
                achievement, 
                interaction.user.display_name
            )
            
            if success:
                await interaction.response.send_message(f"{message} from {user.display_name}.", ephemeral=True)
            else:
                await interaction.response.send_message(
                    message or "Couldn't update that achievement right now. Try again in a moment.",
                    ephemeral=True,
                )
            
        except (sqlite3.Error, discord.HTTPException) as e:
            self.logger.error(
                "Manual remove command failed: user_id=%s achievement_id=%s error=%s",
                user.id,
                achievement,
                e,
                exc_info=True,
            )
            await fail(interaction)
