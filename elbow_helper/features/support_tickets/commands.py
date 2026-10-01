from __future__ import annotations

import io
import logging
from datetime import datetime, timezone

import chat_exporter
import discord
from discord import app_commands
from elbow_helper.discord.embeds import build_status_embed
from elbow_helper.discord.interactions import deny
from elbow_helper.discord.interactions import fail
from elbow_helper.discord.interactions import warn
from elbow_helper.discord.views import TranscriptLinkPromptView

from elbow_helper.configuration.channels import SUPPORT_TICKET_CATEGORY, SUPPORT_TRANSCRIPTS, TICKETS_LOG
from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.configuration.roles import LEAD, RECRUITERS
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX

from .state import load_tickets, save_tickets
from .queries import parse_support_owner_id
from .views import SupportTicketCloseView

LOGGER = logging.getLogger(__name__)


class SupportCommandMixin:
    def prepare_support_close(self, guild: discord.Guild | None,
                              channel: discord.abc.GuildChannel | None,
                              actor: discord.Member) -> dict[str, object]:
        if guild is None or not isinstance(channel, discord.TextChannel):
            return {"issue": "Use this command inside a support ticket."}
        ticket_info = load_tickets().get(str(channel.id))
        if ticket_info is None:
            return {"issue": "This channel is not a ticket created by Elbow Helper."}
        owner = self._resolve_support_ticket_owner(guild, channel, ticket_info)
        source = str(ticket_info.get("source", "reactivation"))
        log_channel_id = SUPPORT_TRANSCRIPTS if source == "open" else TICKETS_LOG
        return {"issue": None, "guild": guild, "channel": channel,
                "actor": actor, "ticket_info": ticket_info,
                "owner": owner, "source": source,
                "log_channel_id": log_channel_id,
                "log_channel": guild.get_channel(log_channel_id),
                "transcript_filename": f"transcript-{channel.name}.html"}

    async def support_close_history(self, channel: discord.TextChannel) -> tuple[tuple[object, ...], ...]:
        rows = []
        async for message in channel.history(limit=None):
            rows.append((
                message.id, message.author.id,
                message.content, message.edited_at.isoformat() if message.edited_at else None,
                tuple((attachment.id, attachment.filename, attachment.url)
                      for attachment in message.attachments),
                tuple(embed.to_dict() for embed in message.embeds),
                tuple((str(reaction.emoji), reaction.count)
                      for reaction in message.reactions),
            ))
        return tuple(rows)

    def support_ticket_target_state(self, guild: discord.Guild,
                                    user: discord.Member, topic: str,
                                    actor: discord.Member) -> dict[str, object]:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        bot_member = guild.me or guild.get_member(self.bot.user.id if self.bot.user else 0)
        if bot_member:
            overwrites[bot_member] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
            )
        visible_roles = []
        for role_id in LEAD:
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True,
                )
                visible_roles.append(role.id)
        display_name = user.display_name or user.name
        return {
            "guild": guild, "user": user, "topic": topic, "actor": actor,
            "name": f"🎫｜support-{display_name}",
            "category": guild.get_channel(SUPPORT_TICKET_CATEGORY),
            "overwrites": overwrites, "visible_roles": visible_roles,
            "bot_member_id": bot_member.id if bot_member else None,
        }

    async def prepare_support_ticket(self, guild: discord.Guild,
                                     user: discord.Member, topic: str,
                                     actor: discord.Member) -> dict[str, object]:
        target = self.support_ticket_target_state(guild, user, topic, actor)
        display_name = user.display_name or user.name
        welcome_text = await self.welcome_messages.create(
            topic or "General assistance", display_name,
        )
        embed = discord.Embed(
            title="Support Ticket",
            description="A staff member will reply soon. You can share any helpful details in the meantime.",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"Opened by {actor.display_name}",
                         icon_url=actor.display_avatar.url)
        return {**target, "welcome": welcome_text, "embed": embed}

    async def open_support_ticket(self, prepared: dict[str, object]):
        guild = prepared["guild"]
        user = prepared["user"]
        ticket_channel = None
        ticket_saved = False
        try:
            ticket_channel = await guild.create_text_channel(
                name=prepared["name"], category=prepared["category"],
                overwrites=prepared["overwrites"],
            )
            await ticket_channel.edit(topic=user.mention)
            await ticket_channel.send(
                content=f"{user.mention}\n{prepared['welcome']}\n",
                embed=prepared["embed"], view=SupportTicketCloseView(self),
            )
            tickets = load_tickets()
            tickets[str(ticket_channel.id)] = {
                "owner": user.id, "topic": prepared["topic"],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source": "open",
            }
            save_tickets(tickets)
            ticket_saved = True
            return ticket_channel, f"Ticket created for {user.mention}: {ticket_channel.mention}"
        except Exception:
            if ticket_channel is not None and not ticket_saved:
                try:
                    await ticket_channel.delete()
                except discord.NotFound:
                    pass
                except (discord.Forbidden, discord.HTTPException):
                    LOGGER.exception(
                        "Failed to remove incomplete support ticket %s",
                        ticket_channel.id,
                    )
            raise

    def support_ticket_registration(self, channel_id: int):
        return load_tickets().get(str(channel_id))

    @staticmethod
    def _build_transcript_link_view() -> discord.ui.View:
        return TranscriptLinkPromptView("support_transcript_link")

    @staticmethod
    def _extract_owner_id_from_topic(topic: str | None) -> int | None:
        return parse_support_owner_id(topic)

    def _resolve_support_ticket_owner(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        ticket_info: dict[str, object],
    ) -> discord.Member | None:
        owner_id = ticket_info.get("owner")
        if isinstance(owner_id, int):
            member = guild.get_member(owner_id)
            if member is not None:
                return member
        topic_owner_id = self._extract_owner_id_from_topic(channel.topic)
        if topic_owner_id is not None:
            return guild.get_member(topic_owner_id)
        return None

    @app_commands.command(name="open", description="Open a support ticket for a member")
    @app_commands.describe(user="Member who needs support", topic="What the member needs help with.")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def open_ticket(self, interaction: discord.Interaction, user: discord.Member, topic: str):
        await interaction.response.defer(ephemeral=True)
        try:
            if not any(role.id in LEAD for role in interaction.user.roles):
                await deny(interaction)
                return

            guild = interaction.guild
            if not guild:
                await interaction.followup.send("Run this in the server, not in DMs.", ephemeral=True)
                return

            prepared = await self.prepare_support_ticket(
                guild, user, topic, interaction.user,
            )
            _, message = await self.open_support_ticket(prepared)
            await interaction.followup.send(message, ephemeral=True)
        except (discord.Forbidden, discord.HTTPException, OSError, RuntimeError, TypeError, ValueError):
            LOGGER.exception("Failed to open ticket")
            try:
                await fail(interaction)
            except discord.HTTPException:
                LOGGER.exception("Failed to send open-ticket failure response")

    async def _handle_close_ticket(self, interaction: discord.Interaction) -> None:
        if not any(role.id in (LEAD | RECRUITERS) for role in interaction.user.roles):
            await deny(interaction)
            return
        prepared = self.prepare_support_close(
            interaction.guild, interaction.channel, interaction.user,
        )
        if prepared["issue"]:
            await warn(interaction, prepared["issue"])
            return
        if not interaction.response.is_done():
            await interaction.response.defer()

        async def safe_followup(message: str) -> None:
            try:
                await interaction.followup.send(message, ephemeral=True)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                LOGGER.warning(
                    "Could not send followup in support close flow for channel %s",
                    prepared["channel"].id,
                )

        await self.close_support_ticket(prepared, safe_followup)

    async def close_support_ticket(self, prepared: dict[str, object],
                                   report) -> dict[str, object]:
        guild = prepared["guild"]
        channel = prepared["channel"]
        actor = prepared["actor"]
        transcript_status_message: discord.Message | None = None
        try:
            await channel.send(
                embed=build_status_embed(
                    f"Ticket Closed by {actor.mention}",
                    discord.Color.gold(),
                )
            )
            transcript_status_message = await channel.send(
                embed=build_status_embed("Saving Transcript", discord.Color.gold())
            )

            owner_member = prepared["owner"]
            if owner_member is not None:
                try:
                    await channel.set_permissions(
                        owner_member,
                        send_messages=False,
                        reason=f"Support ticket closed by {actor}",
                    )
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    LOGGER.warning("Could not lock support ticket %s for owner %s", channel.id, owner_member.id)
            else:
                LOGGER.warning("Could not resolve support ticket owner for channel %s during close.", channel.id)

            transcript = await chat_exporter.export(channel, limit=None, tz_info="UTC")
            if transcript is None:
                await transcript_status_message.edit(
                    embed=build_status_embed("Transcript Couldn't Be Saved", discord.Color.red())
                )
                await report("I couldn't generate the transcript for this ticket. Try again in a moment.")
                return {"status": "partial", "issue": "I couldn't generate the transcript for this ticket. Try again in a moment."}

            transcript_bytes = transcript.encode()
            max_upload_bytes = guild.filesize_limit if guild else 8 * 1024 * 1024
            transcript_file = None
            if len(transcript_bytes) <= max_upload_bytes:
                transcript_file = discord.File(io.BytesIO(transcript_bytes), filename=f"transcript-{channel.name}.html")
            else:
                LOGGER.warning(
                    "Transcript too large to upload for #%s (%s bytes > %s bytes)",
                    channel.name,
                    len(transcript_bytes),
                    max_upload_bytes,
                )

            try:
                messages = [msg async for msg in channel.history(limit=1000)]
            except discord.NotFound:
                LOGGER.warning(
                    "Support ticket channel %s was deleted before history fetch; continuing with 0 messages.",
                    channel.id,
                )
                messages = []

            user_counts: dict[int, int] = {}
            user_labels: dict[int, str] = {}
            for msg in messages:
                user_counts[msg.author.id] = user_counts.get(msg.author.id, 0) + 1
                user_labels.setdefault(
                    msg.author.id,
                    f"{msg.author.mention} - {getattr(msg.author, 'display_name', msg.author.name)}",
                )
            sorted_users = sorted(user_counts.items(), key=lambda row: row[1], reverse=True)
            top_users = sorted_users[:5]

            if channel.topic:
                ticket_owner = channel.topic
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
            detail_embed.add_field(name="Ticket Name", value=channel.name, inline=True)
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
            detail_embed.set_footer(text="Ticket closed • Support Ticket")

            log_channel_id = prepared["log_channel_id"]
            log_channel = guild.get_channel(log_channel_id)
            if log_channel is None:
                await transcript_status_message.edit(
                    embed=build_status_embed("Transcript Couldn't Be Saved", discord.Color.red())
                )
                await report("The transcript log channel hasn't been set up.")
                return {"status": "partial", "issue": "The transcript log channel hasn't been set up."}

            if transcript_file:
                log_message = await log_channel.send(
                    embed=detail_embed,
                    file=transcript_file,
                    view=self._build_transcript_link_view(),
                )
            else:
                log_message = await log_channel.send(embed=detail_embed)

            transcript_saved_text = (
                f"Transcript saved to <#{log_channel_id}>"
                if transcript_file is not None
                else f"Ticket log saved to <#{log_channel_id}>"
            )
            try:
                await transcript_status_message.edit(
                    embed=build_status_embed(transcript_saved_text, discord.Color.green())
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError):
                LOGGER.exception(
                    "Transcript saved, but its status message could not be updated for support ticket %s",
                    channel.id,
                )
                await report(transcript_saved_text)

            try:
                await channel.send(
                    embed=discord.Embed(
                        description="```\nSupport Ticket Controls\n```",
                        color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
                    ),
                    view=self.build_confirm_view(),
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError):
                LOGGER.exception(
                    "Transcript saved, but ticket controls could not be restored for support ticket %s",
                    channel.id,
                )
            return {"status": "complete", "log_channel_id": log_channel_id,
                    "log_message_id": getattr(log_message, "id", None),
                    "transcript_uploaded": transcript_file is not None}
        except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError):
            LOGGER.exception("Failed during close flow for channel %s", channel.id)
            if transcript_status_message is not None:
                try:
                    await transcript_status_message.edit(
                        embed=build_status_embed("Transcript Couldn't Be Saved", discord.Color.red())
                    )
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    LOGGER.warning(
                        "Could not update transcript status message for support ticket %s",
                        channel.id,
                    )
            await report("I couldn't save the transcript. Try again in a moment.")

        return {"status": "partial", "issue": "I couldn't save the transcript. Try again in a moment."}

    async def _reopen_ticket(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        channel = interaction.channel
        owner_member: discord.Member | None = None
        if guild is not None and isinstance(channel, discord.TextChannel):
            tickets = load_tickets()
            ticket_info = tickets.get(str(channel.id), {})
            owner_member = self._resolve_support_ticket_owner(guild, channel, ticket_info)
            if owner_member is not None:
                try:
                    await channel.set_permissions(
                        owner_member,
                        send_messages=True,
                        reason=f"Support ticket reopened by {interaction.user}",
                    )
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    LOGGER.warning(
                        "Could not unlock support ticket %s for owner %s",
                        channel.id,
                        owner_member.id,
                )
            else:
                LOGGER.warning("Could not resolve support ticket owner for channel %s during reopen.", channel.id)

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
        embed.set_footer(text="Support Ticket Controls")
        await interaction.response.edit_message(embed=embed, view=None)

    @app_commands.command(name="close", description="Close this ticket and save the transcript.")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def close(self, interaction: discord.Interaction):
        await self._handle_close_ticket(interaction)
