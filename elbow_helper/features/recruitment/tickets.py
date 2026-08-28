"""Ticket channel maintenance and reminder loops."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
import discord
from discord.ext import commands, tasks
from elbow_helper.discord.channel_ordering import move_text_channel_within_category
from elbow_helper.configuration.channels import REC_ROOM
from elbow_helper.configuration.channels import RECRUITMENT_TICKET_CATEGORY
from elbow_helper.configuration.roles import RECRUITERS

from .config import REMINDER_HOURS
from .config import TICKET_RENAME_EMOJIS
from .config import TICKET_STATUS_ORDER


class TicketMixin:
    @staticmethod
    def _is_transient_discord_network_error(error: Exception) -> bool:
        if isinstance(
            error,
            (
                asyncio.TimeoutError,
                aiohttp.ClientConnectionError,
                aiohttp.ClientConnectorError,
                aiohttp.ClientOSError,
                aiohttp.ServerDisconnectedError,
            ),
        ):
            return True
        if isinstance(error, discord.HTTPException):
            status = getattr(error, "status", None)
            if status in {429, 500, 502, 503, 504}:
                return True
        text = str(error).lower()
        transient_markers = (
            "temporary failure in name resolution",
            "name or service not known",
            "cannot connect to host",
            "connection reset",
            "server disconnected",
            "timed out",
            "timeout",
        )
        return any(marker in text for marker in transient_markers)


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
                await channel.edit(name=f"{TICKET_RENAME_EMOJIS[0]}{new_name}")
        except discord.Forbidden:
            self.logger.warning("Missing permission to rename ticket channel %s (%s)", channel.name, channel.id)
        except discord.HTTPException as e:
            self.logger.warning("Discord API error while auto-renaming %s (%s): %s", channel.name, channel.id, e)
        except (TypeError, ValueError, RuntimeError) as e:
            self.logger.exception("Unexpected auto-rename error for channel %s (%s): %s", channel.name, channel.id, e)

        if getattr(channel, "category_id", None) == RECRUITMENT_TICKET_CATEGORY:
            asyncio.create_task(self._process_applicant_ticket(channel))


    @tasks.loop(minutes=15)
    async def organize_tickets(self):
        # Keep ticket ordering stable by status prefix then creation time.
        try:
            category = self.bot.get_channel(RECRUITMENT_TICKET_CATEGORY)
            if not isinstance(category, discord.CategoryChannel):
                self.logger.warning("Ticket category %s not found or invalid.", RECRUITMENT_TICKET_CATEGORY)
                return
            me = category.guild.me
            if me is None and self.bot.user:
                me = category.guild.get_member(self.bot.user.id)
            if me is None and self.bot.user:
                try:
                    me = await category.guild.fetch_member(self.bot.user.id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    self._warn_ticket_reorder_issue(
                        "member_resolve",
                        "Skipping ticket reorder: unable to resolve bot member object: %s",
                        e,
                    )
                    return
            if me is None:
                self._warn_ticket_reorder_issue(
                    "member_missing",
                    "Skipping ticket reorder: bot member object unavailable.",
                )
                return
            self._clear_ticket_reorder_issue("member_resolve", "member_missing")
            ticket_channels = [
                ch for ch in category.text_channels
                if ch.name[:1] in TICKET_STATUS_ORDER
            ]
            if not ticket_channels:
                self.logger.debug("No ticket channels to organize.")
                return
            def sort_key(ch):
                emoji = ch.name[0]
                status_index = TICKET_STATUS_ORDER.index(emoji) if emoji in TICKET_STATUS_ORDER else len(TICKET_STATUS_ORDER)
                return (status_index, ch.created_at.timestamp())
            ticket_channels.sort(key=sort_key)
            # Recompute ordering from live category state each pass.
            # Moving one channel mutates positions for others, so static plans become stale.
            tracked_ticket_ids = {ch.id for ch in ticket_channels}
            max_passes = max(1, len(ticket_channels) * 2)
            move_attempts = 0
            moved_count = 0
            no_perm_channels: set[str] = set()
            forbidden_channels = []
            forbidden_details = []
            http_failures = []
            for _ in range(max_passes):
                live_ticket_channels = [
                    ch for ch in category.text_channels
                    if ch.id in tracked_ticket_ids and ch.name[:1] in TICKET_STATUS_ORDER
                ]
                if len(live_ticket_channels) != len(tracked_ticket_ids):
                    self.logger.debug(
                        "Ticket set changed during reorder (expected=%s got=%s); stopping pass.",
                        len(tracked_ticket_ids),
                        len(live_ticket_channels),
                    )
                    break

                current_order = sorted(live_ticket_channels, key=lambda ch: ch.position)
                desired_order = sorted(live_ticket_channels, key=sort_key)
                current_ids = [ch.id for ch in current_order]
                desired_ids = [ch.id for ch in desired_order]
                if current_ids == desired_ids:
                    break

                mismatch_indexes = [
                    idx for idx, (current_id, desired_id) in enumerate(zip(current_ids, desired_ids))
                    if current_id != desired_id
                ]
                selected_move: Optional[tuple[int, discord.TextChannel]] = None
                for mismatch_idx in mismatch_indexes:
                    candidate = desired_order[mismatch_idx]
                    perms = candidate.permissions_for(me)
                    if perms.manage_channels and perms.view_channel:
                        selected_move = (mismatch_idx, candidate)
                        break
                    no_perm_channels.add(f"{candidate.name} ({candidate.id})")

                if selected_move is None:
                    # Nothing actionable without required channel permissions.
                    break

                target_index, channel_to_move = selected_move
                current_index = current_order.index(channel_to_move)
                desired_pos = current_order[target_index].position
                if channel_to_move.position == desired_pos:
                    continue

                move_attempts += 1
                try:
                    await move_text_channel_within_category(
                        channel_to_move,
                        category,
                        current_order,
                        target_index,
                        reason="Organize recruitment tickets",
                    )
                    moved_count += 1
                    self.logger.debug("Moved %s to position %s", channel_to_move.name, desired_pos)
                except discord.Forbidden as e:
                    forbidden_channels.append(f"{channel_to_move.name} ({channel_to_move.id})")
                    ch_perms = channel_to_move.permissions_for(me)
                    cat_perms = category.permissions_for(me)
                    perm_snapshot = (
                        f"ch(view={ch_perms.view_channel}, manage={ch_perms.manage_channels}) "
                        f"cat(view={cat_perms.view_channel}, manage={cat_perms.manage_channels})"
                    )
                    err_text = (getattr(e, "text", "") or str(e)).replace("\n", " ").strip()
                    forbidden_details.append(
                        (
                            f"{channel_to_move.name} ({channel_to_move.id}) slot {current_index}->{target_index} "
                            f"pos {channel_to_move.position}->{desired_pos} "
                            f"status={getattr(e, 'status', 'unknown')} "
                            f"code={getattr(e, 'code', 'unknown')} "
                            f"perms={perm_snapshot} err={err_text}"
                        )
                    )
                    break
                except discord.HTTPException as e:
                    http_failures.append((channel_to_move.name, channel_to_move.id, e))
                    break
            else:
                self.logger.debug(
                    "Ticket reorder reached max passes (%s); stopping to avoid churn.",
                    max_passes,
                )

            if no_perm_channels:
                no_perm_sorted = sorted(no_perm_channels)
                preview = ", ".join(no_perm_sorted[:5])
                suffix = f" (+{len(no_perm_sorted) - 5} more)" if len(no_perm_sorted) > 5 else ""
                self._warn_ticket_reorder_issue(
                    "no_manage_channels",
                    "Skipped ticket reorder for %s channels without manage_channels: %s%s",
                    len(no_perm_sorted),
                    preview,
                    suffix,
                )
            else:
                self._clear_ticket_reorder_issue("no_manage_channels")
            if forbidden_channels:
                preview = ", ".join(forbidden_channels[:5])
                suffix = f" (+{len(forbidden_channels) - 5} more)" if len(forbidden_channels) > 5 else ""
                self._warn_ticket_reorder_issue(
                    "forbidden_edits",
                    "Ticket reorder received forbidden on %s channel edits (moved=%s/%s): %s%s",
                    len(forbidden_channels),
                    moved_count,
                    move_attempts,
                    preview,
                    suffix,
                )
                if forbidden_details:
                    details_preview = " | ".join(forbidden_details[:2])
                    details_suffix = f" (+{len(forbidden_details) - 2} more)" if len(forbidden_details) > 2 else ""
                    self.logger.debug("Ticket reorder forbidden details: %s%s", details_preview, details_suffix)
            else:
                self._clear_ticket_reorder_issue("forbidden_edits")
            if http_failures:
                preview = " | ".join(f"{name} ({cid}): {err}" for name, cid, err in http_failures[:3])
                suffix = f" (+{len(http_failures) - 3} more)" if len(http_failures) > 3 else ""
                self.logger.warning(
                    "Ticket reorder had %s Discord API edit failures: %s%s",
                    len(http_failures),
                    preview,
                    suffix,
                )
        except (discord.Forbidden, discord.HTTPException, RuntimeError) as e:
            self.logger.exception("Error in ticket reordering task: %s", e)


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
                if not any(channel.name.startswith(p) for p in ["🚧ticket-", "🤔ticket-"]):
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
                            self.state_store.save_ticket_activity(self.ticket_reminders)
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
                            self.state_store.save_ticket_activity(self.ticket_reminders)
                except discord.Forbidden as e:
                    self.logger.warning("Discord error checking inactive ticket %s (%s): %s", channel.name, channel.id, e)
                except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                    if self._is_transient_discord_network_error(e):
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
                except (TypeError, ValueError, RuntimeError) as e:
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
                self.state_store.save_ticket_activity(self.ticket_reminders)

    @cleanup_old_ticket_reminders.before_loop
    async def before_cleanup_old_ticket_reminders(self):
        await self.bot.wait_until_ready()
