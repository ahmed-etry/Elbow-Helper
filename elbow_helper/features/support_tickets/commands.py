from __future__ import annotations

import io
import logging
import re
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
from .views import SupportTicketCloseView

LOGGER = logging.getLogger(__name__)


class SupportCommandMixin:
    @staticmethod
    def _build_transcript_link_view() -> discord.ui.View:
        return TranscriptLinkPromptView("support_transcript_link")

    @staticmethod
    def _extract_owner_id_from_topic(topic: str | None) -> int | None:
        if not topic:
            return None
        match = re.search(r"<@!?(\d+)>", topic)
        if match:
            return int(match.group(1))
        raw = topic.strip()
        if raw.isdigit():
            return int(raw)
        return None

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

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            }
            bot_member = guild.me or guild.get_member(self.bot.user.id if self.bot.user else 0)
            if bot_member:
                overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
            for role_id in LEAD:
                role = guild.get_role(role_id)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

            display_name = user.display_name or user.name
            ticket_channel = await guild.create_text_channel(
                name=f"🎫｜support-{display_name}",
                category=guild.get_channel(SUPPORT_TICKET_CATEGORY),
                overwrites=overwrites,
            )
            await ticket_channel.edit(topic=user.mention)

            welcome_text = await self.welcome_messages.create(
                topic or "General assistance",
                display_name,
            )
            embed = discord.Embed(
                title="Support Ticket",
                description="A staff member will reply soon. You can share any helpful details in the meantime.",
                color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_footer(text=f"Opened by {interaction.user.display_name}", icon_url=interaction.user.display_avatar.url)
            await ticket_channel.send(
                content=f"{user.mention}\n{welcome_text}\n",
                embed=embed,
                view=SupportTicketCloseView(self),
            )

            tickets = load_tickets()
            tickets[str(ticket_channel.id)] = {
                "owner": user.id,
                "topic": topic,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source": "open",
            }
            save_tickets(tickets)

            await interaction.followup.send(f"Ticket created for {user.mention}: {ticket_channel.mention}", ephemeral=True)
        except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError):
            LOGGER.exception("Failed to open ticket")
            try:
                await fail(interaction)
            except discord.HTTPException:
                LOGGER.exception("Failed to send open-ticket failure response")

    async def _handle_close_ticket(self, interaction: discord.Interaction) -> None:
        if not any(role.id in (LEAD | RECRUITERS) for role in interaction.user.roles):
            await deny(interaction)
            return

        guild = interaction.guild
        channel = interaction.channel
        if guild is None or not isinstance(channel, discord.TextChannel):
            await warn(interaction, "Use this command inside a support ticket.")
            return

        tickets = load_tickets()
        ticket_info = tickets.get(str(channel.id))
        if ticket_info is None:
            await warn(interaction, "This channel is not a ticket created by Elbow Helper.")
            return

        if not interaction.response.is_done():
            await interaction.response.defer()

        async def safe_followup(message: str) -> None:
            try:
                await interaction.followup.send(message, ephemeral=True)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                LOGGER.warning("Could not send followup in support close flow for channel %s", channel.id)

        transcript_status_message: discord.Message | None = None
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

            owner_member = self._resolve_support_ticket_owner(guild, channel, ticket_info)
            if owner_member is not None:
                try:
                    await channel.set_permissions(
                        owner_member,
                        send_messages=False,
                        reason=f"Support ticket closed by {interaction.user}",
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
                await fail(
                    interaction,
                    "I couldn't generate the transcript for this ticket. Try again in a moment.",
                )
                return

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
            source = str(ticket_info.get("source", "reactivation"))
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

            log_channel_id = SUPPORT_TRANSCRIPTS if source == "open" else TICKETS_LOG
            log_channel = guild.get_channel(log_channel_id)
            if log_channel is None:
                await transcript_status_message.edit(
                    embed=build_status_embed("Transcript Couldn't Be Saved", discord.Color.red())
                )
                await safe_followup("The transcript log channel hasn't been set up.")
                return

            if transcript_file:
                await log_channel.send(
                    embed=detail_embed,
                    file=transcript_file,
                    view=self._build_transcript_link_view(),
                )
            else:
                await log_channel.send(embed=detail_embed)

            transcript_saved_text = (
                f"Transcript saved to <#{log_channel_id}>"
                if transcript_file is not None
                else f"Ticket log saved to <#{log_channel_id}>"
            )
            await transcript_status_message.edit(
                embed=build_status_embed(transcript_saved_text, discord.Color.green())
            )
            await channel.send(
                embed=discord.Embed(
                    description="```\nSupport Ticket Controls\n```",
                    color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
                ),
                view=self.build_confirm_view(),
            )
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
            await safe_followup("I couldn't save the transcript. Try again in a moment.")

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
