"""Slash commands for war statements."""

from __future__ import annotations

import logging
import re
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands
from elbow_helper.discord.interactions import deny

from elbow_helper.configuration.roles import LEAD_PLUS

from .statements import CLAN_CHANNEL_MAP, CLAN_CHOICES, WAR_STATEMENTS

LOGGER = logging.getLogger(__name__)

class MemberList(app_commands.Transformer):
    """Transform a space/comma separated list of mentions/IDs into member objects."""

    async def transform(self, interaction: discord.Interaction, value: str) -> List[discord.Member]:
        guild = interaction.guild
        if guild is None:
            return []

        ids = re.findall(r"<@!?(\d+)>|\b(\d{15,20})\b", value)
        flat_ids = []
        for match in ids:
            for part in match:
                if part:
                    flat_ids.append(part)

        seen_ids: set[int] = set()
        members: List[discord.Member] = []
        for uid in flat_ids:
            try:
                user_id = int(uid)
            except (ValueError, TypeError):
                continue
            if user_id in seen_ids:
                continue
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    member = None
            if member:
                seen_ids.add(user_id)
                members.append(member)

        # Fallback: parse tokenized mentions when no members resolved from the first pass.
        if not members:
            for chunk in re.split(r"[\s,]+", value):
                if not chunk:
                    continue
                match = re.match(r"<@!?(\d+)>", chunk)
                if match:
                    uid = int(match.group(1))
                    if uid in seen_ids:
                        continue
                    member = guild.get_member(uid)
                    if member is None:
                        try:
                            member = await guild.fetch_member(uid)
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            member = None
                    if member:
                        seen_ids.add(uid)
                        members.append(member)
        return members


class WarStatements(commands.Cog):
    warstatement = app_commands.Group(
        name="warstatement",
        description="Send ready-made war follow-ups to players.",
    )

    def __init__(self, bot):
        self.bot = bot
        # Predefined statement templates keyed by command name
        self.war_statements = WAR_STATEMENTS

    async def _ensure_lead(self, interaction: discord.Interaction) -> bool:
        # Gate commands to leadership roles
        if any(role.id in LEAD_PLUS for role in interaction.user.roles):
            return True
        await deny(interaction)
        return False

    def _format_notes(self, notes: Optional[str]) -> str:
        return f"\n\n**Additional Notes:** {notes}" if notes else ""

    async def _resolve_clan_channels(
        self, interaction: discord.Interaction, clan: str
    ) -> tuple[Optional[discord.TextChannel], Optional[discord.TextChannel]]:
        # Translate a clan selection into post/clan-war channels.
        mapping = CLAN_CHANNEL_MAP.get(clan)
        if not mapping:
            await interaction.followup.send(
                "Choose a clan from the list.", ephemeral=True
            )
            return None, None

        post_channel = interaction.guild.get_channel(mapping["post_channel"])
        clan_war_channel = interaction.guild.get_channel(mapping["clan_war_channel"])
        if not post_channel or not clan_war_channel:
            await interaction.followup.send(
                "The war statement channel for that clan hasn't been set up. Check that clan's war setup.",
                ephemeral=True,
            )
            return None, None
        return post_channel, clan_war_channel

    async def _send_statement(
        self,
        interaction: discord.Interaction,
        post_channel: discord.TextChannel,
        message: str,
    ):
        # Relay the constructed statement and confirm to the caller
        if len(message) > 2000:
            await interaction.followup.send(
                "Statement is too long. Shorten the notes or reduce the number of players.",
                ephemeral=True,
            )
            return
        try:
            await post_channel.send(message)
        except discord.Forbidden:
            LOGGER.warning(
                "Missing permission to send statement in channel %s",
                getattr(post_channel, "id", None),
            )
            await interaction.followup.send(
                f"I couldn't post in {post_channel.mention}. Check that the bot can send messages there.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            LOGGER.warning(
                "Failed to send statement in channel %s: %s",
                getattr(post_channel, "id", None),
                e,
            )
            await interaction.followup.send(
                "That statement failed to send. Try again in a moment.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"War message sent to {post_channel.mention}.", ephemeral=True
        )

    @warstatement.command(
        name="first-claim",
        description="Ask a player why they attacked someone else's first claim.",
    )
    @app_commands.describe(
        clan="Clan where the message should be posted.",
        victim="Player whose first claim was attacked.",
        attacker="Player who used the attack.",
        notes="Note to add after the message.",
    )
    @app_commands.choices(clan=CLAN_CHOICES)
    async def first_claim(
        self,
        interaction: discord.Interaction,
        clan: str,
        victim: discord.Member,
        attacker: discord.Member,
        notes: Optional[str] = None,
    ):
        if not await self._ensure_lead(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        if victim == attacker:
            await interaction.followup.send(
                "Claim owner and attacker can't be the same player.",
                ephemeral=True,
            )
            return

        post_channel, clan_war_channel = await self._resolve_clan_channels(interaction, clan)
        if not post_channel or not clan_war_channel:
            return

        template = self.war_statements["attacked_first_claim"]["template"]
        message = template.format(
            attacker=attacker.mention,
            victim=victim.mention,
            clan_war_channel=clan_war_channel.mention,
        )
        message += self._format_notes(notes)
        await self._send_statement(interaction, post_channel, message)

    @warstatement.command(
        name="breaking-rules",
        description="Tell players they were removed from war for rule or communication problems.",
    )
    @app_commands.describe(
        clan="Clan where the message should be posted.",
        players="Players receiving the message. Paste mentions or IDs.",
        notes="Note to add after the message.",
    )
    @app_commands.choices(clan=CLAN_CHOICES)
    async def breaking_rules(
        self,
        interaction: discord.Interaction,
        clan: str,
        players: app_commands.Transform[List[discord.Member], MemberList],
        notes: Optional[str] = None,
    ):
        if not await self._ensure_lead(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        if not players:
            await interaction.followup.send(
                "Mention at least one player.", ephemeral=True
            )
            return

        post_channel, _ = await self._resolve_clan_channels(interaction, clan)
        if not post_channel:
            return

        template = self.war_statements["breaking_rules"]["template"]
        message = template.format(users=", ".join(m.mention for m in players))
        message += self._format_notes(notes)
        await self._send_statement(interaction, post_channel, message)

    @warstatement.command(
        name="one-attack-missed",
        description="Ask players why they used only one war attack.",
    )
    @app_commands.describe(
        clan="Clan where the message should be posted.",
        players="Players who missed one attack. Paste mentions or IDs.",
        notes="Note to add after the message.",
    )
    @app_commands.choices(clan=CLAN_CHOICES)
    async def one_attack_missed(
        self,
        interaction: discord.Interaction,
        clan: str,
        players: app_commands.Transform[List[discord.Member], MemberList],
        notes: Optional[str] = None,
    ):
        if not await self._ensure_lead(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        if not players:
            await interaction.followup.send(
                "Mention at least one player.", ephemeral=True
            )
            return

        post_channel, _ = await self._resolve_clan_channels(interaction, clan)
        if not post_channel:
            return

        template = self.war_statements["one_attack_missed"]["template"]
        message = template.format(users=", ".join(m.mention for m in players))
        message += self._format_notes(notes)
        await self._send_statement(interaction, post_channel, message)

    @warstatement.command(
        name="war-filler",
        description="Inform war fillers about their status.",
    )
    @app_commands.describe(
        clan="Clan where the message should be posted.",
        players="War fillers receiving the message. Paste mentions or IDs.",
        notes="Note to add after the message.",
    )
    @app_commands.choices(clan=CLAN_CHOICES)
    async def war_filler(
        self,
        interaction: discord.Interaction,
        clan: str,
        players: app_commands.Transform[List[discord.Member], MemberList],
        notes: Optional[str] = None,
    ):
        if not await self._ensure_lead(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        if not players:
            await interaction.followup.send(
                "Mention at least one player.", ephemeral=True
            )
            return
        max_users = self.war_statements["war_filler"].get("max_users")
        if max_users and len(players) > max_users:
            await interaction.followup.send(
                f"You can include up to {max_users} players in this message.",
                ephemeral=True,
            )
            return

        post_channel, clan_war_channel = await self._resolve_clan_channels(interaction, clan)
        if not post_channel or not clan_war_channel:
            return

        template = self.war_statements["war_filler"]["template"]
        message = template.format(
            users=", ".join(m.mention for m in players),
            clan_war_channel=clan_war_channel.mention,
        )
        message += self._format_notes(notes)
        await self._send_statement(interaction, post_channel, message)

    @warstatement.command(
        name="missed-attacks",
        description="Tell players they were removed from war after missing both attacks.",
    )
    @app_commands.describe(
        clan="Clan where the message should be posted.",
        players="Players who missed both attacks. Paste mentions or IDs.",
        notes="Note to add after the message.",
    )
    @app_commands.choices(clan=CLAN_CHOICES)
    async def missed_attacks(
        self,
        interaction: discord.Interaction,
        clan: str,
        players: app_commands.Transform[List[discord.Member], MemberList],
        notes: Optional[str] = None,
    ):
        if not await self._ensure_lead(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        if not players:
            await interaction.followup.send(
                "Mention at least one player.", ephemeral=True
            )
            return

        post_channel, clan_war_channel = await self._resolve_clan_channels(interaction, clan)
        if not post_channel or not clan_war_channel:
            return

        template = self.war_statements["missed_attacks"]["template"]
        message = template.format(
            users=", ".join(m.mention for m in players),
            clan_war_channel=clan_war_channel.mention,
        )
        message += self._format_notes(notes)
        await self._send_statement(interaction, post_channel, message)

