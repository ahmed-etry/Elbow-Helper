"""Ticket channel maintenance and reminder loops."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord.ext import commands, tasks
from elbow_helper.infrastructure.discord.channel_ordering import reorder_ticket_channels
from elbow_helper.infrastructure.discord.temporary_errors import is_temporary_discord_error
from elbow_helper.configuration.channels import REC_ROOM
from elbow_helper.configuration.channels import RECRUITMENT_TICKET_CATEGORY
from elbow_helper.configuration.roles import RECRUITERS

from .config import REMINDER_HOURS
from .config import TICKET_BASE_PREFIX
from .config import TICKET_OPEN
from .config import TICKET_RENAME_EMOJIS
from .config import TICKET_STATUS_ORDER
from .config import TICKET_TRIAL_ENDED


def _is_ticket_channel(channel: discord.TextChannel) -> bool:
    return channel.name[:1] in TICKET_STATUS_ORDER


def _ticket_sort_key(channel: discord.TextChannel) -> tuple[int, float]:
    return (TICKET_STATUS_ORDER.index(channel.name[0]), channel.created_at.timestamp())


class TicketMixin:
    def _build_inactive_ticket_reminder_embed(
        self,
        channel: discord.TextChannel,
        applicant_id: int,
        last_msg: discord.Message,
        now: datetime,
    ) -> discord.Embed:
        last_ts = int(last_msg.created_at.timestamp())
        embed = discord.Embed(
            title="Applicant Waiting for a Reply",
            description=f"{channel.mention} has been waiting {REMINDER_HOURS} hours for a recruiter reply.",
            color=discord.Color.orange(),
            timestamp=now,
        )
        embed.add_field(name="Ticket", value=channel.mention, inline=True)
        embed.add_field(name="Applicant", value=f"<@{applicant_id}>", inline=True)
        embed.add_field(name="Last Activity", value=f"<t:{last_ts}:R>", inline=True)
        embed.set_footer(text="A new reminder can be sent after the applicant replies again.")
        return embed


    @staticmethod
    def _build_inactive_ticket_reminder_view(last_msg: discord.Message) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Open Ticket", url=last_msg.jump_url))
        return view


    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        # Normalize new ticket names from opener mention and status prefix.
        if not isinstance(channel, discord.TextChannel):
            return
        if channel.category_id != RECRUITMENT_TICKET_CATEGORY:
            return
        if not channel.name.startswith("ticket-"):
            return
        await asyncio.sleep(15)
        try:
            messages = [msg async for msg in channel.history(limit=1, oldest_first=True)]
            if not messages:
                return
            first_msg = messages[0]
            first_line = first_msg.content.strip().splitlines()[0] if first_msg.content else ""
            first_word = first_line.split()[0] if first_line else None
            user_id_match = re.match(r"<@!?(\d+)>", first_word or "")
            if not user_id_match:
                return
            user_id = int(user_id_match.group(1))
            user = channel.guild.get_member(user_id)
            if not user:
                return
            base_name = re.sub(r"[^\w-]", "", user.name.lower())
            new_name = f"ticket-{base_name}"
            if channel.name != new_name:
                await channel.edit(name=new_name)
            if not any(channel.name.startswith(emoji) for emoji in TICKET_RENAME_EMOJIS):
                await channel.edit(name=f"{TICKET_OPEN}{new_name}")
        except discord.Forbidden:
            self.logger.warning("Missing permission to rename ticket channel %s (%s)", channel.name, channel.id)
        except discord.HTTPException as e:
            self.logger.warning("Discord API error while auto-renaming %s (%s): %s", channel.name, channel.id, e)
        except (TypeError, ValueError, RuntimeError) as e:
            self.logger.exception("Unexpected auto-rename error for channel %s (%s): %s", channel.name, channel.id, e)

        if getattr(channel, "category_id", None) == RECRUITMENT_TICKET_CATEGORY:
            self._start_applicant_ticket_processing(channel)


    @tasks.loop(minutes=15)
    async def organize_tickets(self):
        category = self.bot.get_channel(RECRUITMENT_TICKET_CATEGORY)
        if not isinstance(category, discord.CategoryChannel):
            await self._apply_ticket_order()
            return
        tickets = [channel for channel in category.text_channels if _is_ticket_channel(channel)]
        order = tuple(channel.id for channel in sorted(tickets, key=_ticket_sort_key))
        await self.bot.publishing.reorder(
            category.guild.id, (category.id, "text"), order, self._apply_ticket_order,
        )

    async def _apply_ticket_order(self):
        # Keep ticket ordering stable by status prefix then creation time.
        category = self.bot.get_channel(RECRUITMENT_TICKET_CATEGORY)
        if not isinstance(category, discord.CategoryChannel):
            self.logger.warning("Ticket category %s not found or invalid.", RECRUITMENT_TICKET_CATEGORY)
            return False
        return await reorder_ticket_channels(
            category,
            self.bot.user,
            is_ticket=_is_ticket_channel,
            sort_key=_ticket_sort_key,
            reason="Organize recruitment tickets",
            logger=self.logger,
            warn=self._warn_ticket_reorder_issue,
            clear=self._clear_ticket_reorder_issue,
        )


    @organize_tickets.before_loop
    async def before_organize_tickets(self):
        await self.bot.wait_until_ready()


    @tasks.loop(minutes=30)
    async def check_inactive_tickets(self):
        # Escalate once per applicant message when staff has not replied within the threshold.
        now = datetime.now(timezone.utc)
        reminder_channel = self.bot.get_channel(REC_ROOM)
        for guild in self.bot.guilds:
            for channel in guild.text_channels:
                if not any(channel.name.startswith(f"{status}{TICKET_BASE_PREFIX}")
                           for status in (TICKET_OPEN, TICKET_TRIAL_ENDED)):
                    continue
                if channel.category_id != RECRUITMENT_TICKET_CATEGORY:
                    continue
                try:
                    messages = [msg async for msg in channel.history(limit=20, oldest_first=False)]
                    if not messages:
                        continue
                    first_msg = [msg async for msg in channel.history(limit=1, oldest_first=True)][0]
                    mention_match = re.match(r"^<@!?(?P<id>\d+)>", first_msg.content.strip())
                    if not mention_match:
                        continue
                    applicant_id = int(mention_match.group("id"))
                    last_msg = messages[0]
                    if last_msg.author.id != applicant_id:
                        continue
                    async with self._ticket_reminder_lock:
                        reminder_info = self.ticket_reminders.get(channel.id, {})
                        last_reminder = reminder_info.get("last_reminder_at")
                        reminded_applicant_message_id = reminder_info.get("last_applicant_message_id")
                    if reminded_applicant_message_id == last_msg.id:
                        continue
                    if reminded_applicant_message_id is None and last_reminder and last_msg.created_at <= last_reminder:
                        async with self._ticket_reminder_lock:
                            self.ticket_reminders[channel.id] = {
                                **self.ticket_reminders.get(channel.id, {}),
                                "last_applicant_message_id": last_msg.id,
                            }
                            await self.recruitment_state.save_ticket_activity(self.ticket_reminders)
                        continue
                    if last_reminder and (now - last_reminder) < timedelta(hours=REMINDER_HOURS):
                        continue
                    if (now - last_msg.created_at) >= timedelta(hours=REMINDER_HOURS):
                        mentions = " ".join(f"<@&{rid}>" for rid in RECRUITERS)
                        if not reminder_channel:
                            continue
                        embed = self._build_inactive_ticket_reminder_embed(
                            channel,
                            applicant_id,
                            last_msg,
                            now,
                        )
                        view = self._build_inactive_ticket_reminder_view(last_msg)
                        reminder_msg = await reminder_channel.send(
                            f"⏰ Recruitment follow-up needed\n{mentions}",
                            embed=embed,
                            view=view,
                        )
                        async with self._ticket_reminder_lock:
                            self.ticket_reminders[channel.id] = {
                                "last_reminder_at": now,
                                "message_id": reminder_msg.id,
                                "message_channel_id": reminder_channel.id,
                                "last_applicant_message_id": last_msg.id,
                            }
                            await self.recruitment_state.save_ticket_activity(self.ticket_reminders)
                except discord.Forbidden as e:
                    self.logger.warning("Discord error checking inactive ticket %s (%s): %s", channel.name, channel.id, e)
                except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                    if is_temporary_discord_error(e):
                        self._warn_recurring_issue(
                            "inactive_tickets:discord_network",
                            "Skipping inactive ticket sweep: Discord API unreachable while checking %s (%s): %s",
                            channel.name,
                            channel.id,
                            str(e).strip() or e.__class__.__name__,
                            cooldown_seconds=900.0,
                        )
                        return
                    if isinstance(e, discord.HTTPException):
                        self.logger.warning("Discord error checking inactive ticket %s (%s): %s", channel.name, channel.id, e)
                    else:
                        self.logger.exception("Unexpected error checking inactive ticket %s (%s): %s", channel.name, channel.id, e)
                except (TypeError, ValueError, RuntimeError, sqlite3.Error) as e:
                    self.logger.exception("Unexpected error checking inactive ticket %s (%s): %s", channel.name, channel.id, e)
        self._clear_recurring_issue("inactive_tickets:discord_network")


    @check_inactive_tickets.before_loop
    async def before_check_inactive_tickets(self):
        await self.bot.wait_until_ready()


    @tasks.loop(minutes=30)
    async def cleanup_old_ticket_reminders(self):
        # Delete stale reminder messages and clear tracking metadata.
        now = datetime.now(timezone.utc)
        reminders_changed = False
        async with self._ticket_reminder_lock:
            reminders = dict(self.ticket_reminders)
        for channel_id, info in list(reminders.items()):
            last_at = info.get("last_reminder_at")
            if not last_at:
                continue
            if (now - last_at) < timedelta(hours=REMINDER_HOURS):
                continue
            message_id = info.get("message_id")
            message_channel_id = info.get("message_channel_id") or REC_ROOM
            if message_id:
                channel = self.bot.get_channel(message_channel_id)
                if channel:
                    try:
                        msg = await channel.fetch_message(int(message_id))
                        await msg.delete()
                    except discord.NotFound:
                        pass
                    except discord.Forbidden:
                        self.logger.warning(
                            "Missing permission to delete stale reminder message %s in channel %s",
                            message_id,
                            message_channel_id,
                        )
                    except discord.HTTPException as e:
                        self.logger.warning(
                            "Discord API error deleting stale reminder message %s in channel %s: %s",
                            message_id,
                            message_channel_id,
                            e,
                        )
                    except (TypeError, ValueError) as e:
                        self.logger.exception(
                            "Unexpected error deleting stale reminder message %s in channel %s: %s",
                            message_id,
                            message_channel_id,
                            e,
                        )
            if info.get("message_id") is not None or info.get("message_channel_id") is not None:
                info["message_id"] = None
                info["message_channel_id"] = None
                reminders_changed = True
        if reminders_changed:
            async with self._ticket_reminder_lock:
                for channel_id, info in reminders.items():
                    self.ticket_reminders[int(channel_id)] = info
                await self.recruitment_state.save_ticket_activity(self.ticket_reminders)

    @cleanup_old_ticket_reminders.before_loop
    async def before_cleanup_old_ticket_reminders(self):
        await self.bot.wait_until_ready()
