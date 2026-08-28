from __future__ import annotations

import io
import json
import logging
import re
from datetime import datetime, timezone

import chat_exporter
import discord
from elbow_helper.discord.embeds import build_status_embed
from elbow_helper.discord.views import TranscriptLinkPromptView

from elbow_helper.configuration.channels import HIBERNATION_FALLBACK, HIBERNATION_LOG, RECRUITMENT_TICKET_CATEGORY, TICKETS_LOG
from elbow_helper.configuration.files import CREATED_TICKETS_FILE
from elbow_helper.infrastructure.persistence import read_json
from elbow_helper.infrastructure.persistence import write_json_atomic
from elbow_helper.configuration.roles import CORE, RECRUITERS
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX, DEFAULT_THUMBNAIL_URL

from .config import (
    HIBERNATION_CLAN_NAMES,
    FALLBACK_THREAD_ARCHIVE_DURATIONS,
    FALLBACK_THREAD_NAME_LIMIT,
    FALLBACK_THREAD_PREFIX,
)
from .state import (
    extract_owner_id_from_topic,
    get_fallback_info_message_id,
    get_fallback_thread_entry,
    remove_fallback_thread_entry,
    set_fallback_info_message_id,
    set_fallback_thread_entry,
    load_hibernation_state,
    save_hibernation_state,
)

LOGGER = logging.getLogger(__name__)


class HibernationTicketMixin:
    @staticmethod
    def _build_transcript_link_view() -> discord.ui.View:
        return TranscriptLinkPromptView("hibernation_transcript_link")

    @staticmethod
    def _build_hibernation_notice_message(user: discord.Member) -> str:
        return (
            f"Hey {user.mention}, you may have been removed from your clan in-game after "
            "at least two weeks of inactivity.\n\n"
            "You've been moved into hibernation, and your member badge has been removed "
            "until you return. Previous rank roles, such as Elder, won't be restored "
            "automatically.\n\n"
            "When you're ready to return, press the button below. This will restore your "
            "member badge and open a short follow-up ticket to help you rejoin the clan "
            "family. If the button stops working, you can use `/reactivate` in the server."
        )

    @staticmethod
    def _build_fallback_info_embed() -> discord.Embed:
        embed = discord.Embed(
            title="Return from Hibernation",
            description=(
                "If you're currently hibernating, this channel is here to help when you're ready to return.\n\n"
                "**Private Thread**\n"
                "\n"
                "If your hibernation notice couldn't be sent by DM, you'll find it here "
                "with instructions for returning.\n\n"
                "**How to Return**\n"
                "\n"
                "• Open your private thread from the channel list or sidebar and use the button inside.\n\n"
                "• You can also use `/reactivate` at any time, even if you don't have a thread."
            ),
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        return embed

    @staticmethod
    def _fallback_thread_name_for_member(user: discord.Member) -> str:
        display_name = (user.nick or user.display_name or user.name).strip() or str(user.id)
        return f"{FALLBACK_THREAD_PREFIX}{display_name}"[:FALLBACK_THREAD_NAME_LIMIT]

    @staticmethod
    def _fallback_thread_reason(user: discord.Member) -> str:
        return f"Hibernation notice for {user} ({user.id})"

    async def _send_hibernation_fallback_failure(
        self,
        guild: discord.Guild,
        user: discord.Member,
        reason: str,
    ) -> None:
        log_channel = guild.get_channel(HIBERNATION_LOG)
        if not isinstance(log_channel, discord.TextChannel):
            LOGGER.warning("Hibernation log channel %s not found for fallback failure", HIBERNATION_LOG)
            return

        embed = discord.Embed(
            title="Hibernation Notice Couldn't Be Delivered",
            description=reason,
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        embed.add_field(name="Member", value=f"{user.mention} ({user.display_name})", inline=False)
        embed.set_footer(text="Hibernation")
        try:
            await log_channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Failed sending fallback failure log for member %s", user.id)

    async def _fetch_fallback_thread(self, thread_id: int) -> discord.Thread | None:
        channel = self.bot.get_channel(thread_id)
        if isinstance(channel, discord.Thread):
            return channel
        try:
            fetched = await self.bot.fetch_channel(thread_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return fetched if isinstance(fetched, discord.Thread) else None

    async def ensure_fallback_info_message(self, guild: discord.Guild) -> discord.Message | None:
        fallback_channel = guild.get_channel(HIBERNATION_FALLBACK)
        if not isinstance(fallback_channel, discord.TextChannel):
            LOGGER.warning("Fallback channel %s not found for info message", HIBERNATION_FALLBACK)
            return None

        data = load_hibernation_state()
        message_id = get_fallback_info_message_id(data)
        embed = self._build_fallback_info_embed()
        if isinstance(message_id, int):
            try:
                message = await fallback_channel.fetch_message(message_id)
                await message.edit(embed=embed, view=None)
                return message
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                set_fallback_info_message_id(data, None)
                save_hibernation_state(data)

        try:
            message = await fallback_channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Failed posting fallback info message in channel %s", HIBERNATION_FALLBACK)
            return None

        set_fallback_info_message_id(data, message.id)
        save_hibernation_state(data)
        return message

    async def _resolve_or_create_fallback_thread(
        self,
        user: discord.Member,
    ) -> discord.Thread | None:
        guild = user.guild
        await self.ensure_fallback_info_message(guild)
        data = load_hibernation_state()
        existing = get_fallback_thread_entry(data, user.id)
        if existing is not None:
            thread_id = existing.get("thread_id")
            if isinstance(thread_id, int):
                thread = await self._fetch_fallback_thread(thread_id)
                if thread is not None:
                    if thread.archived:
                        try:
                            await thread.edit(
                                name=self._fallback_thread_name_for_member(user),
                                archived=False,
                                locked=False,
                                reason=self._fallback_thread_reason(user),
                            )
                        except (discord.Forbidden, discord.HTTPException) as exc:
                            await self._send_hibernation_fallback_failure(
                                guild,
                                user,
                                f"Couldn't reopen the private hibernation thread <#{thread.id}>: {exc}",
                            )
                            return None
                    elif thread.name != self._fallback_thread_name_for_member(user):
                        try:
                            await thread.edit(
                                name=self._fallback_thread_name_for_member(user),
                                reason=self._fallback_thread_reason(user),
                            )
                        except (discord.Forbidden, discord.HTTPException):
                            LOGGER.warning("Failed renaming fallback thread %s for user %s", thread.id, user.id)
                    try:
                        await thread.fetch_member(user.id)
                    except discord.NotFound:
                        try:
                            await thread.add_user(user)
                        except (discord.Forbidden, discord.HTTPException) as exc:
                            await self._send_hibernation_fallback_failure(
                                guild,
                                user,
                                f"Couldn't add the member back to the private hibernation thread <#{thread.id}>: {exc}",
                            )
                            return None
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        await self._send_hibernation_fallback_failure(
                            guild,
                            user,
                            f"Couldn't check whether the member can access <#{thread.id}>: {exc}",
                        )
                        return None
                    return thread
            remove_fallback_thread_entry(data, user.id)
            save_hibernation_state(data)

        fallback_channel = guild.get_channel(HIBERNATION_FALLBACK)
        if not isinstance(fallback_channel, discord.TextChannel):
            await self._send_hibernation_fallback_failure(
                guild,
                user,
                f"Hibernation notice channel <#{HIBERNATION_FALLBACK}> is missing or isn't a text channel.",
            )
            return None

        thread: discord.Thread | None = None
        last_error: Exception | None = None
        for duration in FALLBACK_THREAD_ARCHIVE_DURATIONS:
            try:
                thread = await fallback_channel.create_thread(
                    name=self._fallback_thread_name_for_member(user),
                    auto_archive_duration=duration,
                    type=discord.ChannelType.private_thread,
                    invitable=False,
                    reason=self._fallback_thread_reason(user),
                )
                break
            except (discord.Forbidden, discord.HTTPException) as exc:
                last_error = exc
                continue

        if thread is None:
            reason = f"Couldn't create a private hibernation thread in {fallback_channel.mention}."
            if last_error is not None:
                reason = f"{reason} Last error: {last_error}"
            await self._send_hibernation_fallback_failure(guild, user, reason)
            return None

        try:
            await thread.add_user(user)
        except (discord.Forbidden, discord.HTTPException) as exc:
            try:
                await thread.delete()
            except (discord.Forbidden, discord.HTTPException):
                LOGGER.warning("Failed cleaning up fallback thread %s after add_user failure", thread.id)
            await self._send_hibernation_fallback_failure(
                guild,
                user,
                f"Created <#{thread.id}>, but couldn't add the member: {exc}",
            )
            return None

        set_fallback_thread_entry(
            data,
            user.id,
            {
                "user_id": user.id,
                "thread_id": thread.id,
                "last_notice_message_id": None,
            },
        )
        save_hibernation_state(data)
        return thread

    async def _post_hibernation_notice_in_fallback_thread(self, user: discord.Member) -> bool:
        thread = await self._resolve_or_create_fallback_thread(user)
        if thread is None:
            return False

        data = load_hibernation_state()
        entry = get_fallback_thread_entry(data, user.id)
        if entry is None:
            entry = {
                "user_id": user.id,
                "thread_id": thread.id,
                "last_notice_message_id": None,
            }
            set_fallback_thread_entry(data, user.id, entry)

        previous_message_id = entry.get("last_notice_message_id")
        if isinstance(previous_message_id, int):
            try:
                previous_message = await thread.fetch_message(previous_message_id)
                await previous_message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        try:
            notice_message = await thread.send(
                self._build_hibernation_notice_message(user),
                view=self._build_reactivate_view(),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            await self._send_hibernation_fallback_failure(
                user.guild,
                user,
                f"Couldn't post the hibernation notice in <#{thread.id}>: {exc}",
            )
            return False

        entry["user_id"] = user.id
        entry["thread_id"] = thread.id
        entry["last_notice_message_id"] = notice_message.id
        set_fallback_thread_entry(data, user.id, entry)
        save_hibernation_state(data)
        return True

    async def _archive_fallback_thread_for_member(
        self,
        user_id: int,
        *,
        reason: str,
    ) -> None:
        data = load_hibernation_state()
        entry = get_fallback_thread_entry(data, user_id)
        if entry is None:
            return
        thread_id = entry.get("thread_id")
        if not isinstance(thread_id, int):
            return

        thread = await self._fetch_fallback_thread(thread_id)
        if thread is None:
            return

        if thread.archived:
            return

        try:
            await thread.edit(archived=True, reason=reason)
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Failed archiving fallback thread %s for user %s", thread_id, user_id)

    async def _delete_fallback_thread_for_member(
        self,
        user_id: int,
    ) -> None:
        data = load_hibernation_state()
        entry = remove_fallback_thread_entry(data, user_id)
        if entry is None:
            save_hibernation_state(data)
            return

        thread_id = entry.get("thread_id")
        if isinstance(thread_id, int):
            thread = await self._fetch_fallback_thread(thread_id)
            if thread is not None:
                try:
                    await thread.delete()
                except (discord.Forbidden, discord.HTTPException):
                    LOGGER.warning("Failed deleting fallback thread %s for user %s", thread_id, user_id)

        save_hibernation_state(data)

    async def _resolve_ticket_owner_from_topic(
        self,
        guild: discord.Guild,
        channel: discord.abc.GuildChannel,
    ) -> discord.Member | None:
        owner_id = extract_owner_id_from_topic(getattr(channel, "topic", None))
        if owner_id is None:
            return None
        member = guild.get_member(owner_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(owner_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    async def _send_hibernation_log(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        stored_role_ids: list[int],
        snapshot_role_ids: list[int],
        unix_ts: int,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        log_channel = guild.get_channel(HIBERNATION_LOG)
        if log_channel is None:
            LOGGER.warning("Hibernation log channel %s not found", HIBERNATION_LOG)
            return

        snapshot_clans = [
            HIBERNATION_CLAN_NAMES[role_id]
            for role_id in stored_role_ids
            if role_id in HIBERNATION_CLAN_NAMES
        ]
        snapshot_rank_names = [
            role.name
            for role_id in snapshot_role_ids
            if (role := guild.get_role(role_id)) is not None
        ]

        embed = discord.Embed(
            title="Member Moved to Hibernation",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        member_name = user.nick or user.name
        actor_name = getattr(interaction.user, "nick", None) or interaction.user.name
        embed.add_field(name="Member", value=f"{user.mention} ({member_name})", inline=False)
        embed.add_field(name="Moved By", value=f"{interaction.user.mention} ({actor_name})", inline=True)
        embed.add_field(name="Date Moved", value=f"<t:{unix_ts}:F>", inline=True)
        embed.add_field(name="Roles Saved", value=str(len(stored_role_ids)), inline=True)
        embed.add_field(name="Previous Clan", value=", ".join(snapshot_clans) if snapshot_clans else "*None*", inline=False)
        embed.add_field(name="Previous Ranks", value=", ".join(snapshot_rank_names) if snapshot_rank_names else "*None*", inline=False)
        embed.set_footer(text="Hibernation")
        await log_channel.send(embed=embed)

    async def _send_hibernation_notice(self, user: discord.Member) -> None:
        message = self._build_hibernation_notice_message(user)
        try:
            await user.send(message, view=self._build_reactivate_view())
        except (discord.Forbidden, discord.HTTPException):
            await self._post_hibernation_notice_in_fallback_thread(user)

    async def _create_reactivation_ticket(
        self,
        guild: discord.Guild,
        actor: discord.Member | discord.User,
        target: discord.Member,
        hibernation_info: dict[str, object],
    ) -> discord.TextChannel:
        bot_member = guild.me or guild.get_member(self.bot.user.id if self.bot.user else 0)
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            target: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        if bot_member is not None:
            overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        for role_id in sorted(RECRUITERS | CORE):
            role = guild.get_role(role_id)
            if role is not None:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        base_name = re.sub(r"[^\w-]", "", target.name.lower()) or str(target.id)
        category = guild.get_channel(RECRUITMENT_TICKET_CATEGORY)
        ticket_channel = await guild.create_text_channel(
            name=f"🔗ticket-{base_name}",
            category=category,
            overwrites=overwrites,
        )
        await ticket_channel.edit(topic=target.mention)

        recruiter_mentions = " ".join(f"<@&{role_id}>" for role_id in sorted(RECRUITERS))
        await ticket_channel.send(f"Welcome back, {target.mention}! {recruiter_mentions} will help you get settled again soon.")

        CREATED_TICKETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            tickets = read_json(CREATED_TICKETS_FILE)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            tickets = {}

        tickets[str(ticket_channel.id)] = {
            "created_by": str(actor.id),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": "reactivation",
        }
        try:
            write_json_atomic(CREATED_TICKETS_FILE, tickets, indent=4)
        except OSError:
            LOGGER.exception("Failed writing %s", CREATED_TICKETS_FILE)

        rank_role_names = [
            guild.get_role(role_id).name
            for role_id in hibernation_info.get("rank_roles", [])
            if guild.get_role(role_id) is not None
        ]
        townhall_roles = [role.name for role in target.roles if role.name.startswith("TH")]
        clan_roles = [
            HIBERNATION_CLAN_NAMES[role.id]
            for role in target.roles
            if role.id in HIBERNATION_CLAN_NAMES
        ]

        embed = discord.Embed(
            title="Reactivation Notice",
            description=(
                "Welcome back! We'll get you sorted into the clan family shortly."
            ),
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        embed.add_field(name="Member", value=target.mention, inline=False)
        if actor.id != target.id:
            embed.add_field(name="Reactivated By", value=actor.mention, inline=False)
        embed.add_field(name="Town Hall Roles", value=", ".join(townhall_roles) or "*None*", inline=False)
        embed.add_field(name="Clan Roles", value=", ".join(clan_roles) or "*None*", inline=False)
        embed.add_field(name="Previous Ranks", value=", ".join(rank_role_names) or "*None*", inline=False)
        embed.add_field(name="Hibernating Since", value=str(hibernation_info["hibernation_date"]), inline=False)
        await ticket_channel.send(embed=embed, view=self._build_close_ticket_view())
        return ticket_channel

    async def _close_reactivation_ticket(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        guild = interaction.guild
        channel_id = getattr(interaction, "channel_id", None) or getattr(channel, "id", 0)
        channel_name = getattr(channel, "name", "unknown-channel")

        async def safe_followup(message: str) -> None:
            try:
                await interaction.followup.send(message, ephemeral=True)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                LOGGER.warning("Could not send followup in close_ticket for channel %s", channel_id)

        if guild is None or channel is None:
            await safe_followup("This ticket channel is no longer available.")
            return

        try:
            await channel.send(
                embed=build_status_embed(
                    f"Ticket Closed by {interaction.user.mention}",
                    discord.Color.gold(),
                )
            )
            transcript_status_message = await channel.send(
                embed=build_status_embed("Saving Transcript", discord.Color.gold())
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LOGGER.warning("Could not post close status messages in ticket %s", channel_id)
            await safe_followup("I couldn't post the ticket close update. Try again in a moment.")
            return

        owner_member = await self._resolve_ticket_owner_from_topic(guild, channel)
        if owner_member is not None:
            try:
                await channel.set_permissions(
                    owner_member,
                    send_messages=False,
                    reason=f"Hibernation ticket closed by {interaction.user}",
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                LOGGER.warning("Could not lock ticket %s for owner %s", channel_id, owner_member.id)
        else:
            LOGGER.warning("Could not resolve ticket owner from topic for channel %s during close.", channel_id)

        try:
            transcript = await chat_exporter.export(
                channel,
                tz_info="UTC",
                military_time=True,
                bot=self.bot,
            )
            if transcript is None:
                try:
                    await transcript_status_message.edit(
                        embed=build_status_embed("Transcript Couldn't Be Saved", discord.Color.red())
                    )
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    LOGGER.warning("Could not update transcript status message in ticket %s", channel_id)
                await safe_followup("I couldn't generate the transcript. Try again in a moment.")
                return

            transcript_bytes = transcript.encode()
            max_upload_bytes = guild.filesize_limit if guild else 8 * 1024 * 1024
            transcript_file = None
            if len(transcript_bytes) <= max_upload_bytes:
                transcript_file = discord.File(io.BytesIO(transcript_bytes), filename=f"transcript-{channel_name}.html")
            else:
                LOGGER.warning(
                    "Transcript too large for upload in #%s (%s bytes > %s bytes)",
                    channel_name,
                    len(transcript_bytes),
                    max_upload_bytes,
                )

            log_channel = guild.get_channel(TICKETS_LOG)
            if log_channel is None:
                try:
                    await transcript_status_message.edit(
                        embed=build_status_embed("Transcript Couldn't Be Saved", discord.Color.red())
                    )
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    LOGGER.warning("Could not update transcript status message in ticket %s", channel_id)
                await safe_followup("The transcript log channel hasn't been set up.")
                return

            try:
                messages = [message async for message in channel.history(limit=1000)]
            except discord.NotFound:
                LOGGER.warning(
                    "Ticket channel %s was deleted before history fetch; continuing with 0 messages.",
                    channel_id,
                )
                messages = []

            user_counts: dict[int, int] = {}
            user_labels: dict[int, str] = {}
            for message in messages:
                user_counts[message.author.id] = user_counts.get(message.author.id, 0) + 1
                user_labels.setdefault(
                    message.author.id,
                    f"{message.author.mention} - {getattr(message.author, 'display_name', message.author.name)}",
                )
            sorted_users = sorted(user_counts.items(), key=lambda item: item[1], reverse=True)
            top_users = sorted_users[:5]

            channel_topic = getattr(channel, "topic", None)
            if channel_topic:
                ticket_owner = channel_topic
            elif top_users:
                ticket_owner = f"<@{top_users[0][0]}>"
            else:
                ticket_owner = "Unknown"

            detail_embed = discord.Embed(
                color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            )
            if owner_member is not None:
                detail_embed.set_author(
                    name=getattr(owner_member, "display_name", owner_member.name),
                    icon_url=owner_member.display_avatar.url,
                )
            else:
                detail_embed.set_author(name="Unknown Ticket Owner")

            detail_embed.add_field(name="Ticket Owner", value=ticket_owner, inline=True)
            detail_embed.add_field(name="Ticket Name", value=channel_name, inline=True)
            detail_embed.add_field(name="Messages", value=str(len(messages)), inline=True)

            participant_lines = []
            for user_id, count in top_users:
                label = user_labels.get(user_id, f"<@{user_id}>")
                message_count = "1 message" if count == 1 else f"{count} messages"
                participant_lines.append(f"{label} — {message_count}")
            if len(sorted_users) > len(top_users):
                additional = len(sorted_users) - len(top_users)
                additional_text = (
                    "+1 more participant"
                    if additional == 1
                    else f"+{additional} more participants"
                )
                participant_lines.append(additional_text)
            detail_embed.add_field(
                name="Participants",
                value="\n".join(participant_lines) if participant_lines else "No participants",
                inline=True,
            )
            detail_embed.set_footer(text="Ticket closed • Reactivation Ticket")

            if transcript_file is not None:
                await log_channel.send(
                    embed=detail_embed,
                    file=transcript_file,
                    view=self._build_transcript_link_view(),
                )
            else:
                await log_channel.send(embed=detail_embed)

        except (discord.Forbidden, discord.HTTPException, OSError, RuntimeError, TypeError, ValueError):
            LOGGER.exception("Failed closing reactivation ticket %s", channel_id)
            try:
                await transcript_status_message.edit(
                    embed=build_status_embed("Transcript Couldn't Be Saved", discord.Color.red())
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                LOGGER.warning("Could not update transcript status message in ticket %s", channel_id)
            await safe_followup("I couldn't save the transcript. Try again in a moment.")
            return

        transcript_saved_text = (
            f"Transcript saved to <#{TICKETS_LOG}>"
            if transcript_file is not None
            else f"Ticket log saved to <#{TICKETS_LOG}>"
        )
        try:
            await transcript_status_message.edit(
                embed=build_status_embed(transcript_saved_text, discord.Color.green())
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LOGGER.warning(
                "Ticket channel %s became unavailable before transcript status could be updated.",
                channel_id,
            )

        confirm_embed = discord.Embed(
            description="```\nReactivation Ticket Controls\n```",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
        )
        try:
            await channel.send(embed=confirm_embed, view=self._build_close_ticket_confirm_view())
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LOGGER.warning(
                "Ticket channel %s became unavailable before close controls could be posted.",
                channel_id,
            )

    async def _reopen_reactivation_ticket(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        channel = interaction.channel
        owner_member: discord.Member | None = None
        if guild is not None and channel is not None:
            owner_member = await self._resolve_ticket_owner_from_topic(guild, channel)
            if owner_member is not None:
                try:
                    await channel.set_permissions(
                        owner_member,
                        send_messages=True,
                        reason=f"Hibernation ticket reopened by {interaction.user}",
                    )
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    LOGGER.warning(
                        "Could not unlock ticket %s for owner %s",
                        getattr(channel, "id", 0),
                        owner_member.id,
                    )
            else:
                LOGGER.warning(
                    "Could not resolve ticket owner from topic for channel %s during reopen.",
                    getattr(channel, "id", 0),
                )

        restored_text = (
            f"Restored messaging access for {owner_member.mention}."
            if owner_member is not None
            else "Messaging access restored."
        )
        embed = discord.Embed(
            title="Ticket Reopened",
            description=restored_text,
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Reopened By", value=interaction.user.mention, inline=True)
        embed.set_footer(text="Reactivation Ticket Controls")
        await interaction.response.edit_message(embed=embed, view=None)

    def _build_close_ticket_view(self) -> discord.ui.View:
        from .views import CloseTicketView

        return CloseTicketView(self)

    def _build_close_ticket_confirm_view(self) -> discord.ui.View:
        from .views import CloseTicketConfirmView

        return CloseTicketConfirmView(self)

    def _build_reactivate_view(self) -> discord.ui.View:
        from .views import ReactivateView

        return ReactivateView(self)
