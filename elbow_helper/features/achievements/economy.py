"""Coin economy helpers, daily drip, salary, and inventory payloads."""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timezone

import discord
from discord.ext import tasks
from elbow_helper.configuration.roles import CORE
from elbow_helper.configuration.roles import ELDER_ROLE_ID
from elbow_helper.configuration.roles import LEAD
from elbow_helper.configuration.roles import LEAD_PLUS
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

class AchievementEconomyMixin:
    def _is_command_runner(self, member: discord.Member) -> bool:
        return any(r.id in LEAD_PLUS for r in getattr(member, "roles", [])) or any(
            r.id in CORE for r in getattr(member, "roles", [])
        )

    def _is_elder(self, member: discord.Member) -> bool:
        return any(r.id == ELDER_ROLE_ID for r in getattr(member, "roles", []))

    def _is_leadership_any(self, member: discord.Member) -> bool:
        return any(r.id in LEAD_PLUS for r in getattr(member, "roles", []))

    async def _maybe_grant_daily(self, message: discord.Message):
        if message.author.bot:
            return
        if not message.guild:
            return
        if self._is_leadership_any(message.author):
            return  # Leadership excluded from coin economy
        content = message.content or ""
        if len(content) < DAILY_MIN_CHARS:
            return
        await asyncio.sleep(1)  # simple debounce to reduce spam-triggering
        await self._retry_db_operation(self._maybe_grant_daily_internal, message)

    async def _maybe_grant_daily_internal(self, cursor, message: discord.Message):
        user_id = message.author.id
        await self._ensure_coin_row(cursor, user_id)
        now = datetime.now(timezone.utc)
        day_key = now.date().toordinal()
        cursor.execute('''
            SELECT daily_msg_day, daily_msg_count, last_daily_ts
            FROM user_coins WHERE user_id = ?
        ''', (user_id,))
        row = cursor.fetchone()
        daily_day = row[0] if row else None
        daily_count = row[1] if row else 0
        last_daily_ts = row[2] if row else None

        if daily_day != day_key:
            daily_day = day_key
            daily_count = 0

        daily_count += 1
        cursor.execute('UPDATE user_coins SET daily_msg_day = ?, daily_msg_count = ? WHERE user_id = ?', (daily_day, daily_count, user_id))

        if daily_count < DAILY_MSG_THRESHOLD:
            return

        if last_daily_ts:
            last_dt = datetime.fromtimestamp(last_daily_ts, tz=timezone.utc)
            if last_dt.date() == now.date():
                return  # already rewarded today

        reward = DAILY_REWARD_ELDER if (self._is_elder(message.author) and not self._is_leadership_any(message.author)) else DAILY_REWARD
        await self._add_coins(cursor, user_id, reward, 'daily', 'daily_drip', None)
        cursor.execute('UPDATE user_coins SET last_daily_ts = ? WHERE user_id = ?', (int(now.timestamp()), user_id))


    def _is_leadership(self, member: discord.Member) -> bool:
        return any(r.id in LEAD for r in getattr(member, "roles", []))

    def _is_core(self, member: discord.Member) -> bool:
        return any(r.id in CORE for r in getattr(member, "roles", []))

    def _require_command_role(self, interaction: discord.Interaction) -> bool:
        return self._is_command_runner(interaction.user)

    def _build_inventory_embed(self, member: discord.Member, balance: int, has_ticket: bool) -> discord.Embed:
        embed = discord.Embed(
            title=f"{member.display_name}'s Inventory",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(timezone.utc),
        )
        raffle_status = "Has a ticket this month" if has_ticket else "No ticket this month"
        embed.description = f"Coins: **{balance}**\nRaffle ticket: **{raffle_status}**"
        embed.set_thumbnail(
            url=DEFAULT_THUMBNAIL_URL
        )
        return embed

    async def _get_inventory_internal(self, cursor, user_id: int):
        cursor.execute("SELECT balance, last_ticket_month FROM user_coins WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        balance = row[0] if row else 0
        last_ticket_month = row[1] if row and len(row) > 1 else None
        has_ticket = last_ticket_month == self._month_key()
        return balance, has_ticket

    async def _coinlog_internal(self, cursor, user_id: int):
        cursor.execute(
            '''
            SELECT amount, type, reason, actor_id, created_at
            FROM coin_transactions
            WHERE user_id = ?
            ORDER BY id DESC
            ''',
            (user_id,),
        )
        return cursor.fetchall()

    @tasks.loop(hours=24)
    async def salary_task(self):
        await self.bot.wait_until_ready()
        guild = self.bot.get_guild(self.GUILD_ID)
        if not guild:
            return
        month_key = self._month_key()
        for member in guild.members:
            if member.bot:
                continue
            if not self._is_elder(member):
                continue
            if self._is_leadership_any(member):
                continue
            await self._retry_db_operation(self._pay_salary_internal, member.id, month_key)

    @salary_task.before_loop
    async def before_salary_task(self):
        await self.bot.wait_until_ready()

    async def _pay_salary_internal(self, cursor, user_id: int, month_key: int):
        await self._ensure_coin_row(cursor, user_id)
        cursor.execute('SELECT last_salary_month FROM user_coins WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        last_month = row[0] if row else None
        if last_month == month_key:
            return
        await self._add_coins(cursor, user_id, SALARY_AMOUNT, 'salary', f'salary_{month_key}', None)
        cursor.execute('UPDATE user_coins SET last_salary_month = ? WHERE user_id = ?', (month_key, user_id))


    async def _grant_coins_internal(self, cursor, member: discord.Member, category: str, amount: int, reason: str, actor: discord.Member):
        user_id = member.id
        await self._ensure_coin_row(cursor, user_id)
        if self._is_leadership_any(member):
            return False, "Leadership users are excluded from the coin system."
        month_key = self._month_key()
        cursor.execute('''
            SELECT manual_cwl_month, manual_cwl_awarded, manual_enc_month, manual_enc_awarded
            FROM user_coins WHERE user_id = ?
        ''', (user_id,))
        row = cursor.fetchone()
        cwl_month, cwl_awarded, enc_month, enc_awarded = row if row else (None, 0, None, 0)
        if cwl_month != month_key:
            cwl_awarded = 0
            cwl_month = month_key
        if enc_month != month_key:
            enc_awarded = 0
            enc_month = month_key
        total_awarded = cwl_awarded + enc_awarded
        override = any(r.id in CORE for r in getattr(actor, "roles", []))
        if category == "cwl":
            if not override and cwl_awarded + amount > MANUAL_CAP_CWL:
                return False, f"CWL cap reached ({cwl_awarded}/{MANUAL_CAP_CWL})."
            if not override and total_awarded + amount > MANUAL_CAP_TOTAL:
                return False, f"Monthly total cap reached ({total_awarded}/{MANUAL_CAP_TOTAL})."
            cwl_awarded += amount
        else:
            if not override and enc_awarded + amount > MANUAL_CAP_ENCOURAGEMENT:
                return False, f"Encouragement cap reached ({enc_awarded}/{MANUAL_CAP_ENCOURAGEMENT})."
            if not override and total_awarded + amount > MANUAL_CAP_TOTAL:
                return False, f"Monthly total cap reached ({total_awarded}/{MANUAL_CAP_TOTAL})."
            enc_awarded += amount

        await self._add_coins(cursor, user_id, amount, f'manual_{category}', reason, actor.id)
        cursor.execute('''
            UPDATE user_coins
            SET manual_cwl_month = ?, manual_cwl_awarded = ?, manual_enc_month = ?, manual_enc_awarded = ?
            WHERE user_id = ?
        ''', (cwl_month, cwl_awarded, enc_month, enc_awarded, user_id))
        coin_word = "coin" if amount == 1 else "coins"
        return True, f"Gave {amount} {coin_word} to {member.display_name} ({category})."

