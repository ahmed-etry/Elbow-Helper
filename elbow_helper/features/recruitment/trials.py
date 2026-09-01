"""Trial lifecycle, reminder, and expiry workflows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import discord
from discord.ext import commands, tasks
from elbow_helper.discord.interactions import deny
from elbow_helper.discord.interactions import fail
from elbow_helper.configuration.channels import REC_ROOM
from elbow_helper.configuration.channels import TRIAL_LIST
from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.configuration.roles import CORE
from elbow_helper.configuration.roles import RECRUITERS
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX
from elbow_helper.configuration.style import DEFAULT_THUMBNAIL_URL

from .config import TRIAL_DAYS_DEFAULT
from .config import TRIAL_RESOLVED_REMINDER_RETENTION_HOURS
from .config import TRIAL_TICKET_PREFIXES_END
from .config import TRIAL_TICKET_PREFIXES_START
from .helpers import can_rename
from .helpers import rename_ticket_channel
from .views import PersistentEndNowView
from .views import PersistentEndTrialView


@dataclass(frozen=True, slots=True)
class TrialStartResult:
    started: bool
    ticket_renamed: bool = True


class TrialMixin:

    async def _rename_trial_ticket(self, channel: discord.TextChannel) -> bool:
        new_name = rename_ticket_channel(channel, TRIAL_TICKET_PREFIXES_START)
        if not new_name:
            return True
        if not can_rename(channel.guild.id):
            return False
        try:
            await channel.edit(name=new_name)
            return True
        except (discord.Forbidden, discord.HTTPException) as error:
            self.logger.warning(
                "Failed to rename channel %s (%s): %s",
                channel.name,
                channel.id,
                error,
            )
            return False

    def _build_trial_tracking_embed(
        self,
        channel: discord.TextChannel,
        applicant_id: int,
        start_dt: datetime,
        end_dt: datetime,
    ) -> discord.Embed:
        """Build the per-trial tracking embed shown in the summary channel."""
        member = channel.guild.get_member(applicant_id) if channel.guild else None
        embed = discord.Embed(
            title="Active Trial",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        embed.add_field(
            name="Applicant",
            value=self._mention_with_nickname(member, applicant_id),
            inline=False,
        )
        embed.add_field(
            name="Ticket",
            value=channel.mention,
            inline=False,
        )
        embed.add_field(
            name="Clans",
            value=self._get_clan_role_mentions(member),
            inline=False,
        )
        embed.add_field(
            name="Ends",
            value=f"<t:{int(end_dt.timestamp())}:R>",
            inline=False,
        )
        embed.add_field(
            name="Started",
            value=f"<t:{int(start_dt.timestamp())}:R>",
            inline=False,
        )
        return embed


    def _build_trial_reminder_embed(
        self,
        ticket_channel_id: int,
        applicant_id: int,
        *,
        resolved_by_id: Optional[int] = None,
        resolved_at_ts: Optional[int] = None,
    ) -> discord.Embed:
        """Build the expired-trial reminder embed shown to recruiters."""
        is_resolved = bool(resolved_by_id and resolved_at_ts)
        embed = discord.Embed(
            title="Trial Ended",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX) if not is_resolved else discord.Color.green(),
        )
        guild = self.bot.get_guild(GUILD_ID)
        applicant_member = guild.get_member(applicant_id) if guild else None
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        embed.add_field(name="Ticket", value=f"<#{ticket_channel_id}>", inline=False)
        embed.add_field(
            name="Applicant",
            value=self._mention_with_nickname(applicant_member, applicant_id),
            inline=False,
        )
        if is_resolved and resolved_by_id and resolved_at_ts:
            resolver_member = guild.get_member(resolved_by_id) if guild else None
            embed.add_field(
                name="Ended by",
                value=self._mention_with_nickname(resolver_member, resolved_by_id),
                inline=False,
            )
            embed.add_field(name="Ended", value=f"<t:{resolved_at_ts}:R>", inline=False)
            embed.add_field(
                name="Status",
                value="Follow-up sent. Waiting for the applicant's reply.",
                inline=False,
            )
        else:
            embed.add_field(
                name="Action",
                value="Hit **End Trial** to send the follow-up.",
                inline=False,
            )
        return embed


    async def _get_trial_reminder_entry(self, ticket_channel_id: int) -> Optional[Dict[str, Any]]:
        """Load one trial reminder entry under lock."""
        async with self._reminder_lock:
            reminders = self.state_store.load_trial_reminders()
            entry = reminders.get(str(ticket_channel_id))
            return dict(entry) if isinstance(entry, dict) else None


    async def _mark_trial_reminder_resolved(
        self,
        *,
        ticket_channel_id: int,
        applicant_id: int,
        resolver_id: int,
        message: Optional[discord.Message] = None,
    ) -> None:
        """Mark a trial reminder as resolved and keep the updated embed visible for 12 hours."""
        resolved_at_ts = int(datetime.now(timezone.utc).timestamp())
        async with self._reminder_lock:
            reminders = self.state_store.load_trial_reminders()
            reminder = reminders.get(str(ticket_channel_id))
            if not isinstance(reminder, dict):
                return
            if reminder.get("resolved_at"):
                return
            reminder["resolved_at"] = resolved_at_ts
            reminder["resolved_by"] = resolver_id
            reminders[str(ticket_channel_id)] = reminder
            self.state_store.save_trial_reminders(reminders)

        if not message:
            return

        resolved_embed = self._build_trial_reminder_embed(
            ticket_channel_id=ticket_channel_id,
            applicant_id=applicant_id,
            resolved_by_id=resolver_id,
            resolved_at_ts=resolved_at_ts,
        )
        disabled_view = PersistentEndTrialView(ticket_channel_id, applicant_id)
        for item in disabled_view.children:
            item.disabled = True

        try:
            await message.edit(content=None, embed=resolved_embed, view=disabled_view)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            self.logger.warning(
                "Could not update resolved trial reminder message %s for ticket %s: %s",
                message.id,
                ticket_channel_id,
                e,
            )


    async def _delete_tracking_message(
        self,
        trial_info: Dict[str, Any],
        *,
        ticket_channel_id: Optional[int] = None,
    ) -> bool:
        """Delete a trial's tracked message."""
        tracking_msg_id = trial_info.get("tracking_msg_id")
        tracking_channel_id = trial_info.get("tracking_channel_id")
        if not tracking_msg_id or not tracking_channel_id:
            return True

        applicant_id = trial_info.get("applicant_id")
        channel = self.bot.get_channel(int(tracking_channel_id))
        if not channel:
            try:
                channel = await self.bot.fetch_channel(int(tracking_channel_id))
            except discord.NotFound:
                return True
            except (discord.Forbidden, discord.HTTPException):
                return False
        if not isinstance(channel, discord.TextChannel):
            return False

        try:
            msg = await channel.fetch_message(int(tracking_msg_id))
            await msg.delete()
            return True
        except discord.NotFound:
            pass
        except (discord.Forbidden, discord.HTTPException):
            return False

        # Recover when the stored message ID no longer resolves.
        try:
            async for msg in channel.history(limit=80, oldest_first=False):
                if not (msg.author and msg.author.bot and msg.embeds):
                    continue
                emb = msg.embeds[0]
                title = (emb.title or "").lower()
                if "active trial" not in title:
                    continue
                blob = " ".join(
                    [
                        emb.title or "",
                        emb.description or "",
                        " ".join(f"{f.name} {f.value}" for f in emb.fields),
                    ]
                ).lower()
                if ticket_channel_id and str(ticket_channel_id) in blob:
                    await msg.delete()
                    return True
                if not ticket_channel_id and applicant_id and str(applicant_id) in blob:
                    await msg.delete()
                    return True
        except (discord.Forbidden, discord.HTTPException):
            return False
        return True


    async def _delete_reminder_entry(self, ticket_channel_id: int) -> None:
        """Delete stored reminder entry for a ticket, if present."""
        async with self._reminder_lock:
            reminders = self.state_store.load_trial_reminders()
            if reminders.pop(str(ticket_channel_id), None) is not None:
                self.state_store.save_trial_reminders(reminders)


    async def _delete_reminder_message(self, ticket_channel_id: int, reminder_info: Dict[str, Any]) -> None:
        """Delete a reminder message and clear its entry."""
        channel_id = reminder_info.get("channel_id")
        message_id = reminder_info.get("message_id")
        if not channel_id or not message_id:
            await self._delete_reminder_entry(ticket_channel_id)
            return
        channel = self.bot.get_channel(int(channel_id))
        if channel:
            try:
                msg = await channel.fetch_message(int(message_id))
                await msg.delete()
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException) as e:
                self.logger.warning(
                    "Could not delete reminder message %s in channel %s: %s",
                    message_id,
                    channel_id,
                    e,
                )
                return
        await self._delete_reminder_entry(ticket_channel_id)


    async def start_trial_for_accept(
        self,
        channel: discord.TextChannel,
        days: int,
        applicant_id: int,
    ) -> TrialStartResult:
        """Start a trial using the same behavior as /trial, but driven by /accept."""
        async with self._trial_lock:
            trials = self.state_store.load_trial_data()
            existing = trials.get(str(channel.id))
            if isinstance(existing, dict):
                try:
                    existing_applicant_id = int(existing.get("applicant_id"))
                except (TypeError, ValueError):
                    existing_applicant_id = None
                if existing_applicant_id == applicant_id:
                    return TrialStartResult(
                        started=True,
                        ticket_renamed=await self._rename_trial_ticket(channel),
                    )
                self.logger.error(
                    "Trial start refused: ticket %s already tracks applicant %s",
                    channel.id,
                    existing_applicant_id,
                )
                return TrialStartResult(started=False)

            tracking_channel = self.bot.get_channel(TRIAL_LIST)
            if not tracking_channel:
                self.logger.error("Trial start failed: tracking channel not found")
                return TrialStartResult(started=False)

            start_dt = datetime.now(timezone.utc)
            trial_end = start_dt + timedelta(days=days)

            # Tracking embed carries trial metadata and actionable controls.
            tracking_embed = self._build_trial_tracking_embed(
                channel,
                applicant_id,
                start_dt,
                trial_end,
            )
            tracking_msg = await tracking_channel.send(
                embed=tracking_embed,
                view=PersistentEndNowView(channel.guild.id, channel.id, applicant_id),
            )

            # Persist trial state for loops and persistent views.
            trial_entry = {
                "start": start_dt.isoformat(),
                "days": days,
                "applicant_id": applicant_id,
                "tracking_msg_id": tracking_msg.id,
                "tracking_channel_id": tracking_channel.id,
            }
            trials[str(channel.id)] = trial_entry
            try:
                self.state_store.save_trial_data(trials)
            except (OSError, TypeError, ValueError):
                try:
                    await tracking_msg.delete()
                except discord.NotFound:
                    pass
                except (discord.Forbidden, discord.HTTPException):
                    self.logger.warning(
                        "Could not remove untracked trial message %s for ticket %s",
                        tracking_msg.id,
                        channel.id,
                    )
                raise

            return TrialStartResult(
                started=True,
                ticket_renamed=await self._rename_trial_ticket(channel),
            )


    async def end_trial_now(
        self,
        interaction: discord.Interaction,
        ticket_channel_id: int,
        applicant_id: int,
        allow_missing: bool = False,
        *,
        show_success_confirmation: bool = True,
    ) -> bool:
        """End a trial early from the tracking message button."""
        user_roles = getattr(interaction.user, "roles", ())
        if not any(getattr(role, "id", None) in (CORE | RECRUITERS) for role in user_roles):
            await deny(interaction)
            return False

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=show_success_confirmation)

        async with self._trial_lock:
            trial_key = str(ticket_channel_id)
            trials = self.state_store.load_trial_data()
            trial_info = trials.get(trial_key)
            if not trial_info and not allow_missing:
                await interaction.followup.send("This ticket no longer has an active trial.", ephemeral=True)
                return False

            ticket_channel = self.bot.get_channel(ticket_channel_id)
            if not ticket_channel:
                await interaction.followup.send("The trial ticket is no longer available.", ephemeral=True)
                return False
            if not applicant_id:
                applicant_id = await self._resolve_applicant_id(ticket_channel)
                if not applicant_id:
                    await interaction.followup.send(
                        "The applicant for this ticket couldn't be identified.", ephemeral=True
                    )
                    return False

            rename_notice: Optional[str] = None
            new_name = rename_ticket_channel(ticket_channel, TRIAL_TICKET_PREFIXES_END)
            if new_name and new_name != ticket_channel.name:
                guild_id = ticket_channel.guild.id
                if len(new_name) > 100:
                    rename_notice = "The channel wasn't renamed because the new name exceeds Discord's 100-character limit."
                elif can_rename(guild_id):
                    try:
                        await ticket_channel.edit(name=new_name)
                    except discord.Forbidden:
                        rename_notice = "The channel wasn't renamed because the bot can't manage this channel."
                    except discord.HTTPException as e:
                        if e.status == 429:
                            rename_notice = "Discord temporarily limited channel renames. Try again shortly."
                        elif e.code == 50035:
                            rename_notice = "Discord rejected the new channel name, so the channel wasn't renamed."
                        else:
                            rename_notice = f"Discord couldn't rename the channel."
                        self.logger.warning(
                            "Failed to rename channel %s (%s): %s",
                            ticket_channel.name,
                            ticket_channel.id,
                            e,
                        )
                else:
                    rename_notice = "The channel was renamed too recently. Try again later."

            if trial_info:
                tracking_removed = await self._delete_tracking_message(
                    trial_info,
                    ticket_channel_id=ticket_channel_id,
                )
                if not tracking_removed:
                    self.logger.warning(
                        "Trial end retained state because tracking cleanup failed for ticket %s",
                        ticket_channel_id,
                    )
                    await fail(interaction)
                    return False

            feedback_notice: Optional[str] = None
            try:
                await ticket_channel.send(
                    f"Hey <@{applicant_id}>, your trial has ended. Are you planning to stay with us? "
                    "We'd also appreciate any feedback about how it went."
                )
            except discord.Forbidden:
                feedback_notice = "The bot couldn't post the trial follow-up because it can't send messages in the ticket."
            except discord.HTTPException as e:
                feedback_notice = f"The bot couldn't post the trial follow-up in the ticket."
                self.logger.warning(
                    "Failed to send trial end prompt in channel %s (%s): %s",
                    ticket_channel.name,
                    ticket_channel.id,
                    e,
                )

            if trial_info:
                trials.pop(trial_key, None)
                self.state_store.save_trial_data(trials)

            confirmation_lines = ["Trial ended."] if show_success_confirmation else []
            if rename_notice:
                confirmation_lines.append(rename_notice)
            if feedback_notice:
                confirmation_lines.append(feedback_notice)

            if confirmation_lines:
                await interaction.followup.send("\n".join(confirmation_lines), ephemeral=True)
            return True


    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Remove active-trial tracking when its applicant leaves."""
        async with self._trial_lock:
            trials = self.state_store.load_trial_data()
            matching: list[tuple[str, Dict[str, Any]]] = []
            for ticket_id, info in trials.items():
                if not isinstance(info, dict):
                    continue
                try:
                    applicant_id = int(info.get("applicant_id"))
                except (TypeError, ValueError):
                    continue
                if applicant_id == member.id:
                    matching.append((ticket_id, dict(info)))

        cleaned: list[tuple[str, Dict[str, Any]]] = []
        for ticket_id, info in matching:
            try:
                removed = await self._delete_tracking_message(
                    info,
                    ticket_channel_id=int(ticket_id),
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                self.logger.exception(
                    "Trial cleanup failed for departed member %s ticket %s",
                    member.id,
                    ticket_id,
                )
                continue
            if removed:
                cleaned.append((ticket_id, info))
            else:
                self.logger.warning(
                    "Trial tracking cleanup incomplete for departed member %s ticket %s",
                    member.id,
                    ticket_id,
                )

        if not cleaned:
            return

        async with self._trial_lock:
            trials = self.state_store.load_trial_data()
            changed = False
            for ticket_id, original in cleaned:
                current = trials.get(ticket_id)
                if current == original:
                    trials.pop(ticket_id, None)
                    changed = True
            if changed:
                self.state_store.save_trial_data(trials)


    @tasks.loop(minutes=60)
    async def check_expired_trials(self):
        """Hourly sweep to identify trials past their duration and notify staff."""
        now = datetime.now(timezone.utc)
        reminder_channel = self.bot.get_channel(REC_ROOM)
        if not reminder_channel:
            return

        expired: list[tuple[int, Dict[str, Any]]] = []
        async with self._trial_lock:
            trials = self.state_store.load_trial_data()
            for ticket_id, info in list(trials.items()):
                try:
                    start_dt = datetime.fromisoformat(info["start"]).replace(tzinfo=timezone.utc)
                    days = int(info.get("days", TRIAL_DAYS_DEFAULT))
                    if now >= start_dt + timedelta(days=days):
                        expired.append((int(ticket_id), dict(info)))
                except (KeyError, TypeError, ValueError) as e:
                    self.logger.warning("Skipping invalid trial entry %s: %s", ticket_id, e)
                    continue

        for ticket_id, info in expired:
            async with self._trial_lock:
                trial_key = str(ticket_id)
                trials = self.state_store.load_trial_data()
                if trials.get(trial_key) != info:
                    continue

                ticket_channel = self.bot.get_channel(ticket_id)
                if not ticket_channel:
                    self.logger.warning(
                        "Expired trial retained because ticket %s is unavailable",
                        ticket_id,
                    )
                    continue

                applicant_id = info.get("applicant_id", 0)
                async with self._reminder_lock:
                    reminders = self.state_store.load_trial_reminders()
                    reminder_entry = reminders.get(trial_key)
                    if not isinstance(reminder_entry, dict):
                        mentions = " ".join(f"<@&{rid}>" for rid in RECRUITERS)
                        mention_prefix = f"{mentions} " if mentions else ""
                        reminder_embed = self._build_trial_reminder_embed(
                            ticket_id,
                            applicant_id,
                        )
                        reminder_msg = await reminder_channel.send(
                            f"{mention_prefix}Trial period ended for {ticket_channel.mention}.",
                            embed=reminder_embed,
                            view=PersistentEndTrialView(ticket_id, applicant_id),
                        )
                        reminders[trial_key] = {
                            "channel_id": reminder_channel.id,
                            "message_id": reminder_msg.id,
                            "applicant_id": applicant_id,
                            "created_at": int(datetime.now(timezone.utc).timestamp()),
                            "resolved_at": None,
                            "resolved_by": None,
                        }
                        try:
                            self.state_store.save_trial_reminders(reminders)
                        except (OSError, TypeError, ValueError):
                            try:
                                await reminder_msg.delete()
                            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                                self.logger.warning(
                                    "Could not remove untracked trial reminder %s for ticket %s",
                                    reminder_msg.id,
                                    ticket_id,
                                )
                            raise

                tracking_removed = await self._delete_tracking_message(
                    info,
                    ticket_channel_id=ticket_id,
                )
                if not tracking_removed:
                    self.logger.warning(
                        "Expired trial retained because tracking cleanup failed for ticket %s",
                        ticket_id,
                    )
                    continue

                trials.pop(trial_key, None)
                self.state_store.save_trial_data(trials)


    @check_expired_trials.before_loop
    async def before_check_expired_trials(self):
        await self.bot.wait_until_ready()


    @tasks.loop(minutes=30)
    async def cleanup_trial_reminders(self):
        """Delete resolved reminders after retention; keep unresolved reminders visible."""
        now_ts = int(datetime.now(timezone.utc).timestamp())
        async with self._reminder_lock:
            reminders = self.state_store.load_trial_reminders()
        if not reminders:
            return
        to_delete = []
        for ticket_id, info in reminders.items():
            resolved_at = int(info.get("resolved_at", 0) or 0)
            if resolved_at and now_ts - resolved_at >= (TRIAL_RESOLVED_REMINDER_RETENTION_HOURS * 3600):
                to_delete.append((ticket_id, info))
        for ticket_id, info in to_delete:
            await self._delete_reminder_message(int(ticket_id), info)


    @cleanup_trial_reminders.before_loop
    async def before_cleanup_trial_reminders(self):
        await self.bot.wait_until_ready()
