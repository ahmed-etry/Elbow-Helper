"""Raffle state, hub rendering, and draw/ticket internals."""

from __future__ import annotations

import asyncio
import random
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Optional

import discord
from discord.ext import tasks
from elbow_helper.configuration.channels import GIVEAWAYS
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX
from elbow_helper.configuration.style import DEFAULT_THUMBNAIL_URL

from .config import RAFFLE_END_CUTOFF_DAYS
from .config import RAFFLE_HUB_UPDATE_HOURS
from .config import TICKET_COST
from .views import RaffleHubView

RAFFLE_HUB_HTTP_RETRY_DELAYS_SECONDS = (1.0, 2.0, 5.0)


class AchievementRaffleMixin:
    def raffle_draw_month(self, month: Optional[str]):
        current_month_key = self._month_key()
        if not month:
            return current_month_key, None, False
        target_month_key = self._parse_month_label(month.strip())
        if target_month_key is None:
            return None, "That doesn't look like a valid month. Use the format YYYY-MM, like 2025-09.", True
        if target_month_key > current_month_key:
            return None, "You can't draw raffle winners for a future month.", False
        return target_month_key, None, False

    def raffle_month_label(self, month_key: int):
        return self._month_label_from_key(month_key)

    async def raffle_draw_state(self, guild_id: int, month_key: int):
        return await self._retry_db_operation(
            self._raffle_draw_state_internal, guild_id, month_key,
        )

    async def _raffle_draw_state_internal(self, cursor, guild_id, month_key):
        tickets, eligible = await self._eligible_raffle_tickets(cursor, guild_id, month_key)
        cursor.execute(
            'SELECT user_id, is_active FROM raffle_winners WHERE month_key = ? ORDER BY id',
            (month_key,),
        )
        rows = cursor.fetchall()
        winners_raw = await self._get_meta(cursor, f"winners_{month_key}")
        try:
            winners_count = max(1, int(winners_raw)) if winners_raw else 1
        except (TypeError, ValueError):
            winners_count = 1
        reward = await self._get_meta(cursor, f"reward_{month_key}")
        return {"month_key": month_key, "tickets": tickets, "eligible": eligible,
                "winners": [row[0] for row in rows],
                "active_winners": [row[0] for row in rows if row[1]],
                "winners_count": winners_count,
                "reward": reward, "current_month_key": self._month_key()}

    def raffle_draw_issue(self, state, *, reroll: bool = False):
        month_key = state["month_key"]
        month_scope = (
            "this month" if month_key == state["current_month_key"]
            else self._month_label_from_key(month_key)
        )
        if reroll:
            if not state["active_winners"]:
                return "Raffle has not been drawn yet."
        elif state["winners"]:
            return f"Raffle already drawn for {month_scope}."
        elif not state["tickets"]:
            return f"No tickets for {month_scope}."
        elif not state["eligible"]:
            return f"No eligible tickets for {month_scope}."
        eligible_count = len(state["eligible"])
        winners_count = state["winners_count"]
        if eligible_count < winners_count:
            ticket_label = f"{eligible_count} eligible ticket" + ("" if eligible_count == 1 else "s")
            winner_label = f"{winners_count} winner" + ("" if winners_count == 1 else "s")
            winner_verb = "is" if winners_count == 1 else "are"
            scope = "This month" if reroll else month_scope.capitalize()
            return f"{scope} has only {ticket_label}, but {winner_label} {winner_verb} set."
        return None

    async def _eligible_raffle_tickets(self, cursor, guild_id, month_key):
        cursor.execute('SELECT user_id FROM raffle_tickets WHERE month_key = ?', (month_key,))
        tickets = [row[0] for row in cursor.fetchall()]
        guild = self.bot.get_guild(guild_id)
        eligible = []
        for user_id in tickets:
            member = guild.get_member(user_id) if guild else None
            if member and not self._is_leadership_any(member):
                eligible.append(user_id)
        return tickets, eligible

    async def draw_raffle_winners(self, guild_id: int, month_key: int, post):
        ok, winners, info = await self._retry_db_operation(
            self._draw_raffle_internal, guild_id, month_key,
        )
        if not ok:
            return False, info
        winner_mentions = ", ".join(f"<@{uid}>" for uid in winners)
        reward = await self._retry_db_operation(self._get_meta, f"reward_{month_key}")
        reward_text = reward.strip() if isinstance(reward, str) and reward.strip() else "Prize not set"
        announcement = (
            f"Congratulations, {winner_mentions}! You won this month's raffle prize: **{reward_text}**. "
            f"Contact <@327057918992187395> to collect it."
        )
        await post(announcement)
        await self.update_raffle_hub_message()
        return True, announcement

    async def reroll_raffle_winners(self, guild_id: int, post):
        ok, winners, info = await self._retry_db_operation(
            self._raffle_reroll_internal, guild_id,
        )
        if not ok:
            return False, info
        winner_mentions = ", ".join(f"<@{winner_id}>" for winner_id in winners)
        result_label = "New result" if len(winners) == 1 else "New results"
        embed = discord.Embed(
            title="Raffle Redrawn",
            description=f"{result_label}: {winner_mentions}",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(timezone.utc),
        )
        await post(embed)
        await self.update_raffle_hub_message()
        return True, embed

    async def raffle_member_ticket_state(self, user_id: int):
        return await self._retry_db_operation(
            self._raffle_member_ticket_state_internal, user_id,
        )

    async def _raffle_member_ticket_state_internal(self, cursor, user_id: int):
        month_key = self._month_key()
        cursor.execute(
            'SELECT 1 FROM raffle_tickets WHERE month_key = ? AND user_id = ?',
            (month_key, user_id),
        )
        has_ticket = cursor.fetchone() is not None
        cursor.execute(
            'SELECT last_ticket_month FROM user_coins WHERE user_id = ?', (user_id,),
        )
        row = cursor.fetchone()
        return {"month_key": month_key, "has_ticket": has_ticket,
                "last_ticket_month": row[0] if row else None}

    async def remove_raffle_ticket(self, user_id: int):
        return await self._retry_db_operation(self._raffle_remove_internal, user_id)

    async def raffle_clear_state(self):
        return await self._retry_db_operation(self._raffle_clear_state_internal)

    async def _raffle_clear_state_internal(self, cursor):
        month_key = self._month_key()
        cursor.execute(
            'SELECT user_id FROM raffle_tickets WHERE month_key = ? ORDER BY user_id',
            (month_key,),
        )
        tickets = [row[0] for row in cursor.fetchall()]
        cursor.execute(
            'SELECT user_id FROM raffle_winners WHERE month_key = ? ORDER BY id',
            (month_key,),
        )
        winners = [row[0] for row in cursor.fetchall()]
        return {"month_key": month_key, "tickets": tickets, "winners": winners}

    async def clear_raffle(self, clear_tickets: bool):
        message = await self._retry_db_operation(
            self._raffle_clear_internal, clear_tickets,
        )
        await self.update_raffle_hub_message()
        return message

    async def ticket_grant_state(self, user_id: int):
        return await self._retry_db_operation(self._ticket_grant_state_internal, user_id)

    async def _ticket_grant_state_internal(self, cursor, user_id: int):
        month_key = self._month_key()
        is_open, status = await self._raffle_is_open_internal(cursor, month_key)
        cursor.execute(
            'SELECT last_ticket_month FROM user_coins WHERE user_id = ?', (user_id,),
        )
        row = cursor.fetchone()
        has_ticket = row is not None and row[0] == month_key
        guild = self.bot.get_guild(self.GUILD_ID)
        member = guild.get_member(user_id) if guild else None
        issue = status if not is_open else None
        if issue is None and has_ticket:
            issue = "User already has a ticket this month."
        if issue is None and member and self._is_leadership_any(member):
            issue = "Leadership cannot hold raffle tickets."
        return {"month_key": month_key, "has_ticket": has_ticket,
                "open": is_open, "issue": issue}

    async def grant_raffle_ticket(self, user_id: int, reason: str):
        ok, message = await self._retry_db_operation(
            self._grant_ticket_internal, user_id, reason,
        )
        if ok:
            await self.update_raffle_hub_message()
        return ok, message

    async def _raffle_list_internal(self, cursor):
        month_key = self._month_key()
        cursor.execute('SELECT user_id FROM raffle_tickets WHERE month_key = ?', (month_key,))
        return [r[0] for r in cursor.fetchall()]

    async def _raffle_remove_internal(self, cursor, user_id: int):
        month_key = self._month_key()
        cursor.execute('DELETE FROM raffle_tickets WHERE month_key = ? AND user_id = ?', (month_key, user_id))
        cursor.execute('SELECT last_ticket_month FROM user_coins WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        if row and row[0] == month_key:
            cursor.execute('UPDATE user_coins SET last_ticket_month = NULL WHERE user_id = ?', (user_id,))
        cursor.execute('''
            INSERT INTO coin_transactions (user_id, amount, type, reason, actor_id, created_at)
            VALUES (?, 0, ?, ?, ?, ?)
        ''', (user_id, 'ticket_remove', f'ticket_removed_{month_key}', None, int(datetime.now(timezone.utc).timestamp())))
        return "Ticket removed for this month (if any)."


    def _raffle_hub_end_timestamp(self, month_key: Optional[int] = None) -> int:
        if month_key is None:
            month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            year = (month_key - 1) // 12
            month = month_key - (year * 12)
            month_start = datetime(year, month, 1, tzinfo=timezone.utc)
        next_month = (month_start + timedelta(days=32)).replace(day=1)
        last_day = next_month - timedelta(days=1)
        end_dt = last_day - timedelta(days=RAFFLE_END_CUTOFF_DAYS)
        end_dt = end_dt.replace(hour=23, minute=59, second=0, microsecond=0, tzinfo=timezone.utc)
        return int(end_dt.timestamp())

    async def _raffle_hub_snapshot_internal(self, cursor, month_key: int):
        reward = await self._get_meta(cursor, f"reward_{month_key}")
        winners = await self._get_meta(cursor, f"winners_{month_key}")
        cursor.execute('SELECT COUNT(*) FROM raffle_tickets WHERE month_key = ?', (month_key,))
        ticket_count = cursor.fetchone()[0]
        cursor.execute(
            '''
            SELECT user_id, drawn_at, is_active
            FROM raffle_winners
            WHERE month_key = ?
            ORDER BY drawn_at ASC, id ASC
            ''',
            (month_key,),
        )
        winner_rows = cursor.fetchall()
        return reward, winners, ticket_count, winner_rows

    async def _raffle_is_open_internal(self, cursor, month_key: int) -> tuple[bool, Optional[str]]:
        state, hub_month = await self._get_raffle_hub_state_internal(cursor)
        if hub_month is None:
            hub_month = month_key
        current_reward = (await self._get_meta(cursor, f"reward_{month_key}") or "").strip()
        previous_month_key = month_key - 1
        _, _, previous_ticket_count, previous_winner_rows = await self._raffle_hub_snapshot_internal(cursor, previous_month_key)
        previous_pending_draw = previous_ticket_count > 0 and not previous_winner_rows

        if not current_reward and previous_pending_draw:
            previous_month_label = self._month_label_from_key(previous_month_key)
            return False, f"{previous_month_label} raffle is still pending draw."
        cursor.execute('SELECT 1 FROM raffle_winners WHERE month_key = ? LIMIT 1', (month_key,))
        if cursor.fetchone():
            return False, "Raffle already drawn this month."
        if int(datetime.now(timezone.utc).timestamp()) >= self._raffle_hub_end_timestamp(month_key):
            return False, "Raffle entries are closed for this month."
        if state == "ended" and hub_month != month_key:
            return False, "Raffle is not open yet. Prize not set for this month."
        if state == "ended" and hub_month == month_key:
            return False, "Raffle already drawn this month."
        return True, None

    def _is_transient_discord_http_error(self, exc: discord.HTTPException) -> bool:
        status = getattr(exc, "status", None)
        return status == 429 or (isinstance(status, int) and 500 <= status < 600)

    async def _maybe_retry_raffle_hub_http_error(
        self,
        exc: discord.HTTPException,
        *,
        message_id: int,
        operation: str,
        attempt: int,
    ) -> bool:
        total_attempts = len(RAFFLE_HUB_HTTP_RETRY_DELAYS_SECONDS) + 1
        status = getattr(exc, "status", "unknown")
        if not self._is_transient_discord_http_error(exc):
            self.logger.exception(
                "Failed to %s raffle hub message id=%s status=%s; keeping stored message id",
                operation,
                message_id,
                status,
            )
            return False
        if attempt >= len(RAFFLE_HUB_HTTP_RETRY_DELAYS_SECONDS):
            self.logger.exception(
                "Transient Discord HTTP error during %s for raffle hub message id=%s status=%s after %s attempts; keeping stored message id",
                operation,
                message_id,
                status,
                total_attempts,
            )
            return False

        delay = RAFFLE_HUB_HTTP_RETRY_DELAYS_SECONDS[attempt]
        self.logger.warning(
            "Transient Discord HTTP error during %s for raffle hub message id=%s status=%s attempt=%s/%s; retrying in %.1f seconds",
            operation,
            message_id,
            status,
            attempt + 1,
            total_attempts,
            delay,
        )
        await asyncio.sleep(delay)
        return True

    async def _try_update_existing_raffle_hub_message(
        self,
        channel: discord.TextChannel,
        message_id: int,
        *,
        embed: discord.Embed,
        view: discord.ui.View,
    ) -> Optional[bool]:
        for attempt in range(len(RAFFLE_HUB_HTTP_RETRY_DELAYS_SECONDS) + 1):
            try:
                message = await channel.fetch_message(message_id)
            except (discord.NotFound, discord.Forbidden):
                return False
            except discord.HTTPException as exc:
                if await self._maybe_retry_raffle_hub_http_error(
                    exc,
                    message_id=message_id,
                    operation="fetch",
                    attempt=attempt,
                ):
                    continue
                return None

            try:
                await message.edit(embed=embed, view=view)
                return True
            except (discord.NotFound, discord.Forbidden):
                return False
            except discord.HTTPException as exc:
                if await self._maybe_retry_raffle_hub_http_error(
                    exc,
                    message_id=message_id,
                    operation="edit",
                    attempt=attempt,
                ):
                    continue
                return None

        return None

    async def update_raffle_hub_message(self, force_new: bool = False):
        await self.bot.wait_until_ready()
        async with self._raffle_hub_lock:
            guild = self.bot.get_guild(self.GUILD_ID)
            if not guild:
                return
            channel = guild.get_channel(GIVEAWAYS)
            if not channel or not isinstance(channel, discord.TextChannel):
                return

            month_key = self._month_key()
            state, hub_month = await self._retry_db_operation(self._get_raffle_hub_state_internal)
            if hub_month is None:
                hub_month = month_key
                await self._retry_db_operation(self._set_raffle_hub_state_internal, "active", hub_month)
                state = "active"
            if state is None:
                state = "active"
                await self._retry_db_operation(self._set_raffle_hub_state_internal, state, hub_month)

            current_reward = (await self._retry_db_operation(self._get_meta, f"reward_{month_key}") or "").strip()
            previous_month_key = month_key - 1
            _, _, previous_ticket_count, previous_winner_rows = await self._retry_db_operation(
                self._raffle_hub_snapshot_internal,
                previous_month_key,
            )
            previous_pending_draw = previous_ticket_count > 0 and not previous_winner_rows
            previous_drawn = bool(previous_winner_rows)

            # Keep the cycle on the previous month until that month is drawn when no reward is set for live month.
            if not current_reward and previous_pending_draw:
                if hub_month != previous_month_key or state != "active":
                    hub_month = previous_month_key
                    state = "active"
                    await self._retry_db_operation(self._set_raffle_hub_state_internal, state, hub_month)
            elif hub_month != month_key and state == "active":
                # Active raffles should track the live month once the previous cycle is resolved/configured.
                hub_month = month_key
                await self._retry_db_operation(self._set_raffle_hub_state_internal, state, hub_month)

            # Recovery safety: if live month is not configured and previous month is already drawn, keep that result visible.
            if not current_reward and previous_drawn and not previous_pending_draw:
                if hub_month != previous_month_key or state != "ended":
                    state = "ended"
                    hub_month = previous_month_key
                    await self._retry_db_operation(self._set_raffle_hub_state_internal, state, hub_month)

            display_month_key = hub_month if hub_month != month_key else month_key
            reward, winners_raw, ticket_count, winner_rows = await self._retry_db_operation(
                self._raffle_hub_snapshot_internal,
                display_month_key,
            )
            active_winners = [row[0] for row in winner_rows if row[2] == 1]
            drawn_at = winner_rows[0][1] if winner_rows else None
            raffle_drawn = bool(winner_rows)
            if raffle_drawn and state != "ended":
                state = "ended"
                hub_month = display_month_key
                await self._retry_db_operation(self._set_raffle_hub_state_internal, state, hub_month)

            reward_text = reward if reward else "Prize not set"
            try:
                winners = max(1, int(winners_raw)) if winners_raw else 1
            except (TypeError, ValueError):
                winners = 1

            end_ts = self._raffle_hub_end_timestamp(display_month_key)
            now_ts = int(datetime.now(timezone.utc).timestamp())
            entries_closed = now_ts >= end_ts
            month_label = self._month_label_from_key(display_month_key)
            embed = discord.Embed(
                title=f"{month_label} Raffle",
                color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
                timestamp=datetime.now(timezone.utc),
            )
            if state == "ended" and hub_month == display_month_key and raffle_drawn:
                winners_text = ", ".join(f"<@{uid}>" for uid in active_winners) if active_winners else "None"
                drawn_text = f"<t:{int(drawn_at)}:R>" if drawn_at else "Completed"
                embed.description = (
                    f"Prize: **{reward_text}**\n"
                    f"Entries: **{ticket_count:,}**\n"
                    f"Winners: **{winners:,}**\n"
                    "Draw complete.\n"
                    f"Current winners: {winners_text}\n"
                    f"Drawn {drawn_text}."
                )
                buy_enabled = False
            elif entries_closed:
                embed.description = (
                    f"Prize: **{reward_text}**\n"
                    f"Entries: **{ticket_count:,}**\n"
                    f"Winners: **{winners:,}**\n"
                    f"Entries closed <t:{end_ts}:F>.\n"
                    "Draw pending."
                )
                buy_enabled = False
            else:
                embed.description = (
                    f"Prize: **{reward_text}**\n"
                    f"Entries: **{ticket_count:,}**\n"
                    f"Winners: **{winners:,}**\n"
                    f"Entry cost: **{TICKET_COST:,} coins**"
                )
                buy_enabled = True

            embed.set_thumbnail(
                url=DEFAULT_THUMBNAIL_URL
            )
            
            embed.set_footer(text="Use /economyinfo for coins, tickets, and raffle details.")

            message_id = None if force_new else await self._retry_db_operation(
                self._get_meta, "raffle_hub_message_id"
            )
            view = RaffleHubView(self, buy_enabled=buy_enabled)
            if message_id:
                update_result = await self._try_update_existing_raffle_hub_message(
                    channel,
                    int(message_id),
                    embed=embed,
                    view=view,
                )
                if update_result is True:
                    return
                if update_result is False:
                    message_id = None
                else:
                    return

            message = await channel.send(embed=embed, view=view)
            await self._retry_db_operation(self._set_meta, "raffle_hub_message_id", str(message.id))
            await self._retry_db_operation(self._set_raffle_hub_state_internal, state, hub_month)

    @tasks.loop(hours=RAFFLE_HUB_UPDATE_HOURS)
    async def raffle_hub_task(self):
        await self.update_raffle_hub_message()

    @raffle_hub_task.before_loop
    async def before_raffle_hub_task(self):
        await self.bot.wait_until_ready()

    # Ticket purchase logic used by the raffle hub button.
    async def raffle_purchase_state(self, user_id: int):
        """Read a ticket purchase's cost, balance and eligibility without changing them."""
        return await self._retry_db_operation(self._raffle_purchase_state_internal, user_id)

    async def _raffle_purchase_state_internal(self, cursor, user_id: int):
        month_key = self._month_key()
        is_open, issue = await self._raffle_is_open_internal(cursor, month_key)
        cursor.execute(
            'SELECT last_ticket_month, balance FROM user_coins WHERE user_id = ?',
            (user_id,),
        )
        row = cursor.fetchone()
        last_ticket_month, balance = row if row else (None, 0)
        guild = self.bot.get_guild(self.GUILD_ID)
        member = guild.get_member(user_id) if guild else None
        if is_open and last_ticket_month == month_key:
            issue = "You already have a ticket this month."
        elif is_open and member and self._is_leadership_any(member):
            issue = "Leadership cannot hold raffle tickets."
        elif is_open and balance < TICKET_COST:
            issue = f"Not enough coins. Need {TICKET_COST}, you have {balance}."
        return {"month_key": month_key, "cost": TICKET_COST,
                "balance": balance, "has_ticket": last_ticket_month == month_key,
                "issue": issue}

    async def buy_raffle_ticket(self, user_id: int):
        """Purchase a ticket for the panel and agent."""
        ok, message = await self._retry_db_operation(self._buy_ticket_internal, user_id)
        if ok:
            await self.update_raffle_hub_message()
        return ok, message

    async def _buy_ticket_internal(self, cursor, user_id: int):
        await self._ensure_coin_row(cursor, user_id)
        month_key = self._month_key()
        is_open, status_msg = await self._raffle_is_open_internal(cursor, month_key)
        if not is_open:
            return False, status_msg
        cursor.execute('SELECT last_ticket_month, balance FROM user_coins WHERE user_id = ?', (user_id,))
        last_ticket_month, balance = cursor.fetchone()
        if last_ticket_month == month_key:
            return False, "You already have a ticket this month."
        guild = self.bot.get_guild(self.GUILD_ID)
        if guild:
            member = guild.get_member(user_id)
            if member and self._is_leadership_any(member):
                return False, "Leadership cannot hold raffle tickets."
        if balance < TICKET_COST:
            return False, f"Not enough coins. Need {TICKET_COST}, you have {balance}."
        await self._add_coins(cursor, user_id, -TICKET_COST, 'raffle_purchase', f'ticket_{month_key}', user_id)
        cursor.execute('UPDATE user_coins SET last_ticket_month = ? WHERE user_id = ?', (month_key, user_id))
        cursor.execute('INSERT OR REPLACE INTO raffle_tickets (month_key, user_id) VALUES (?, ?)', (month_key, user_id))
        return True, "Ticket purchased for this month."


    async def _grant_ticket_internal(self, cursor, user_id: int, reason: str):
        await self._ensure_coin_row(cursor, user_id)
        state = await self._ticket_grant_state_internal(cursor, user_id)
        if state["issue"]:
            return False, state["issue"]
        month_key = state["month_key"]
        cursor.execute('UPDATE user_coins SET last_ticket_month = ? WHERE user_id = ?', (month_key, user_id))
        cursor.execute('INSERT OR REPLACE INTO raffle_tickets (month_key, user_id) VALUES (?, ?)', (month_key, user_id))
        cursor.execute('''
            INSERT INTO coin_transactions (user_id, amount, type, reason, actor_id, created_at)
            VALUES (?, 0, ?, ?, ?, ?)
        ''', (user_id, 'ticket_grant', reason, None, int(datetime.now(timezone.utc).timestamp())))
        return True, "Ticket granted for this month."

    async def _raffle_reroll_internal(self, cursor, guild_id: int):
        month_key = self._month_key()
        state = await self._raffle_draw_state_internal(cursor, guild_id, month_key)
        issue = self.raffle_draw_issue(state, reroll=True)
        if issue:
            return False, None, issue
        eligible = state["eligible"]
        winners_count = state["winners_count"]

        winners = random.sample(eligible, winners_count)
        drawn_at = int(datetime.now(timezone.utc).timestamp())
        cursor.execute(
            'DELETE FROM raffle_winners WHERE month_key = ?',
            (month_key,),
        )
        cursor.execute(
            """
            DELETE FROM coin_transactions
            WHERE type IN ('raffle_win', 'raffle_reroll')
              AND reason IN (?, ?)
            """,
            (f"raffle_{month_key}", f"raffle_{month_key}_reroll"),
        )
        for winner in winners:
            cursor.execute(
                """
                INSERT INTO raffle_winners (month_key, user_id, drawn_at, draw_type, reroll_of, is_active)
                VALUES (?, ?, ?, 'draw', NULL, 1)
                """,
                (month_key, winner, drawn_at),
            )
            cursor.execute(
                """
                INSERT INTO coin_transactions (user_id, amount, type, reason, actor_id, created_at)
                VALUES (?, 0, 'raffle_win', ?, NULL, ?)
                """,
                (winner, f"raffle_{month_key}", drawn_at),
            )
        await self._set_raffle_hub_state_internal(cursor, "ended", month_key)
        return True, winners, "ok"


    async def _raffle_history_internal(self, cursor, month_key: int):
        reward = await self._get_meta(cursor, f"reward_{month_key}")
        winners_raw = await self._get_meta(cursor, f"winners_{month_key}")
        cursor.execute(
            '''
            SELECT user_id, drawn_at, draw_type, reroll_of, is_active
            FROM raffle_winners
            WHERE month_key = ?
            ORDER BY drawn_at ASC, id ASC
            ''',
            (month_key,),
        )
        return cursor.fetchall(), reward, winners_raw


    async def _draw_raffle_internal(self, cursor, guild_id: int, month_key: Optional[int] = None):
        current_month_key = self._month_key()
        target_month_key = month_key if month_key is not None else current_month_key
        state = await self._raffle_draw_state_internal(
            cursor, guild_id, target_month_key,
        )
        issue = self.raffle_draw_issue(state)
        if issue:
            return False, None, issue
        eligible = state["eligible"]
        winners_count = state["winners_count"]

        winners = random.sample(eligible, winners_count)
        drawn_at = int(datetime.now(timezone.utc).timestamp())
        for winner in winners:
            cursor.execute(
                '''
                INSERT INTO raffle_winners (month_key, user_id, drawn_at, draw_type, reroll_of, is_active)
                VALUES (?, ?, ?, 'draw', NULL, 1)
                ''',
                (target_month_key, winner, drawn_at),
            )
            cursor.execute('''
                INSERT INTO coin_transactions (user_id, amount, type, reason, actor_id, created_at)
                VALUES (?, 0, ?, ?, ?, ?)
            ''', (winner, 'raffle_win', f'raffle_{target_month_key}', None, drawn_at))

        await self._set_meta(cursor, "last_winner_month", str(target_month_key))
        if target_month_key == current_month_key:
            await self._set_raffle_hub_state_internal(cursor, "ended", target_month_key)
        else:
            # Keep the hub pinned to the recovered month only when the live month is not configured yet.
            current_reward = (await self._get_meta(cursor, f"reward_{current_month_key}") or "").strip()
            if not current_reward:
                await self._set_raffle_hub_state_internal(cursor, "ended", target_month_key)
        return True, winners, "ok"

    async def _raffle_clear_internal(self, cursor, clear_tickets: bool):
        month_key = self._month_key()
        if clear_tickets:
            cursor.execute('DELETE FROM raffle_tickets WHERE month_key = ?', (month_key,))
            cursor.execute('UPDATE user_coins SET last_ticket_month = NULL WHERE last_ticket_month = ?', (month_key,))
        cursor.execute('DELETE FROM raffle_winners WHERE month_key = ?', (month_key,))
        await self._set_raffle_hub_state_internal(cursor, "active", month_key)
        if clear_tickets:
            return "Cleared this month's raffle winners and tickets."
        else:
            return "Cleared this month's raffle winners."
