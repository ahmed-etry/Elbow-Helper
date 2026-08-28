"""Persistent CWL roster hub and member-specific routing views."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Dict
from typing import List
from typing import Optional
from typing import Set

import discord
from discord.ext import commands

from elbow_helper.features.rosters.config import CWL_CLAN_ROSTER_IDS
from elbow_helper.features.rosters.models import LinkedAccount
from elbow_helper.features.rosters.models import Roster
from elbow_helper.features.rosters.models import RosterMember
from elbow_helper.features.rosters.services.profiles import fetch_account_profiles
from elbow_helper.configuration.channels import CLAN_CWL_INFO_CHANNELS
from elbow_helper.configuration.channels import CLAN_TRANSFERS
from elbow_helper.configuration.channels import CLAN_WAR_CHANNELS
from elbow_helper.configuration.channels import CWL_FULL_ROSTERS_THREAD
from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX
from elbow_helper.configuration.style import DEFAULT_THUMBNAIL_URL

from .config import CLAN_LINKS
from .config import CWL_CLAN_CODES
from .config import CWL_CLAN_NAMES
from .config import CWL_CLAN_TAGS
from .helpers import wait_for_boot_complete


LOGGER = logging.getLogger(__name__)
TRANSFER_HUB_TITLE = "CWL Rosters and Transfers"
TRANSFER_HUB_RETRY_DELAYS_SECONDS = (0.0, 2.0, 5.0)


class CwlTransferHubMixin:
    @staticmethod
    def _full_rosters_url() -> str:
        return f"https://discord.com/channels/{GUILD_ID}/{CWL_FULL_ROSTERS_THREAD}"

    def _transfer_hub_url(self) -> Optional[str]:
        message_id = self.transfer_state.get("hub_message_id")
        if not isinstance(message_id, int) or message_id <= 0:
            return None
        return f"https://discord.com/channels/{GUILD_ID}/{CLAN_TRANSFERS}/{message_id}"

    @staticmethod
    def _build_transfer_hub_embed(
        guild: Optional[discord.Guild] = None,
        *,
        placements_released: bool = True,
    ) -> discord.Embed:
        if placements_released:
            description = (
                "See where you’re playing for CWL and whether\n"
                "you still need to move.\n\n"
                "Find your CWL info and war discussion channels,\n"
                "or browse the full rosters."
            )
        else:
            description = "The CWL rosters haven’t been announced yet."
        embed = discord.Embed(
            title=TRANSFER_HUB_TITLE,
            description=description,
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
        )
        icon_url = (
            str(guild.icon.url)
            if guild is not None and guild.icon is not None
            else DEFAULT_THUMBNAIL_URL
        )
        embed.set_thumbnail(url=icon_url)
        return embed

    def _build_transfer_hub_view(self, *, placements_released: bool) -> discord.ui.View:
        from .views import CwlTransferHubView

        return CwlTransferHubView(self, placements_released=placements_released)

    async def _current_cwl_roster_cycles(
        self,
        guild_id: int,
    ) -> Optional[Dict[str, int]]:
        try:
            rosters = await self.roster_queries.list_for_guild(guild_id)
        except (OSError, sqlite3.Error):
            LOGGER.exception("Could not read CWL roster cycles guild=%s", guild_id)
            return None

        rosters_by_id = {roster.id: roster for roster in rosters}
        cycles: Dict[str, int] = {}
        for clan_code, roster_id in CWL_CLAN_ROSTER_IDS.items():
            roster = rosters_by_id.get(roster_id)
            if roster is None or roster.clan_code != clan_code:
                LOGGER.warning(
                    "CWL roster cycle is unavailable clan=%s roster_id=%s",
                    clan_code,
                    roster_id,
                )
                return None
            cycles[str(roster_id)] = roster.active_cycle_id or 0
        return cycles

    async def _cwl_placements_release_status(self, guild_id: int) -> Optional[bool]:
        current_cycles = await self._current_cwl_roster_cycles(guild_id)
        if current_cycles is None:
            return None
        released_cycles = self.transfer_state.get("released_roster_cycles")
        return bool(current_cycles and current_cycles == released_cycles)

    def _release_cwl_placements(self, roster_cycles: Dict[str, int]) -> None:
        self.transfer_state["released_roster_cycles"] = dict(roster_cycles)
        self._save_transfer_state()

    def _is_transfer_hub_message(self, message: discord.Message) -> bool:
        bot_user = self.bot.user
        return bool(
            bot_user
            and message.author.id == bot_user.id
            and any(embed.title == TRANSFER_HUB_TITLE for embed in message.embeds)
        )

    async def _bootstrap_transfer_hub(self) -> None:
        await wait_for_boot_complete(self.bot)
        for delay in TRANSFER_HUB_RETRY_DELAYS_SECONDS:
            if delay:
                await asyncio.sleep(delay)
            if await self.ensure_transfer_hub():
                return

    async def ensure_transfer_hub(self) -> bool:
        async with self._transfer_hub_lock:
            channel = self.bot.get_channel(CLAN_TRANSFERS)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(CLAN_TRANSFERS)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                    LOGGER.warning("Could not resolve the CWL transfer hub channel: %s", error)
                    return False
            if not isinstance(channel, discord.TextChannel):
                LOGGER.warning("CWL transfer hub channel is not a text channel: %s", CLAN_TRANSFERS)
                return False

            release_status = await self._cwl_placements_release_status(channel.guild.id)
            placements_released = release_status is True
            if release_status is False and self.transfer_state.get("released_roster_cycles"):
                self.transfer_state["released_roster_cycles"] = {}
                self._save_transfer_state()
            embed = self._build_transfer_hub_embed(
                channel.guild,
                placements_released=placements_released,
            )
            view = self._build_transfer_hub_view(
                placements_released=placements_released,
            )
            message_id = self.transfer_state.get("hub_message_id")
            if message_id:
                try:
                    message = await channel.fetch_message(int(message_id))
                    await message.edit(content=None, embed=embed, view=view)
                    return True
                except discord.NotFound:
                    self.transfer_state["hub_message_id"] = None
                    self._save_transfer_state()
                except (discord.Forbidden, discord.HTTPException) as error:
                    LOGGER.warning(
                        "Could not update CWL transfer hub message %s: %s",
                        message_id,
                        error,
                    )
                    return False

            try:
                async for message in channel.history(limit=100):
                    if not self._is_transfer_hub_message(message):
                        continue
                    await message.edit(content=None, embed=embed, view=view)
                    self.transfer_state["hub_message_id"] = message.id
                    self._save_transfer_state()
                    return True
            except (discord.Forbidden, discord.HTTPException) as error:
                LOGGER.warning("Could not look for an existing CWL transfer hub: %s", error)
                return False

            try:
                message = await channel.send(embed=embed, view=view)
            except (discord.Forbidden, discord.HTTPException) as error:
                LOGGER.warning("Could not post the CWL transfer hub: %s", error)
                return False
            self.transfer_state["hub_message_id"] = message.id
            self._save_transfer_state()
            return True

    @commands.Cog.listener()
    async def on_roster_cycle_opened(self, roster: Roster) -> None:
        if roster.id not in CWL_CLAN_ROSTER_IDS.values():
            return
        released_cycles = self.transfer_state.get("released_roster_cycles")
        if not isinstance(released_cycles, dict) or not released_cycles:
            return
        if released_cycles.get(str(roster.id)) == (roster.active_cycle_id or 0):
            return
        await self.ensure_transfer_hub()

    async def _member_cwl_assignments(
        self,
        guild_id: Optional[int],
        discord_user_id: int,
    ) -> Optional[Dict[str, List[RosterMember]]]:
        if guild_id is None:
            return None

        roster_ids = tuple(CWL_CLAN_ROSTER_IDS.values())
        try:
            guild_rosters = await self.roster_queries.list_for_guild(guild_id)
            members_by_roster = await self.roster_queries.members_for_user(
                roster_ids,
                discord_user_id,
            )
        except (OSError, sqlite3.Error):
            LOGGER.exception("Could not read CWL assignments for member=%s", discord_user_id)
            return None

        rosters_by_id = {roster.id: roster for roster in guild_rosters}
        assignments: Dict[str, List[RosterMember]] = {}
        for clan_code in CWL_CLAN_CODES:
            roster_id = CWL_CLAN_ROSTER_IDS.get(clan_code)
            if roster_id is None:
                continue
            roster = rosters_by_id.get(roster_id)
            if roster is None or roster.clan_code != clan_code:
                LOGGER.warning(
                    "CWL roster mapping is unavailable clan=%s roster_id=%s",
                    clan_code,
                    roster_id,
                )
                return None
            member_accounts = members_by_roster.get(roster_id, [])
            if member_accounts:
                assignments[clan_code] = member_accounts
        return assignments

    @staticmethod
    def _safe_member_text(value: str) -> str:
        return discord.utils.escape_mentions(discord.utils.escape_markdown(value))

    @staticmethod
    def _join_account_blocks(blocks: List[str], limit: int = 1024) -> str:
        value = ""
        for block in blocks:
            candidate = block if not value else f"{value}\n\n{block}"
            if len(candidate) <= limit:
                value = candidate
                continue
            suffix = "\n…"
            if value:
                return f"{value[:limit - len(suffix)]}{suffix}"
            return f"{block[:limit - 1]}…"
        return value

    def _build_member_cwl_embed(
        self,
        assignments: Dict[str, List[RosterMember]],
        profiles: Dict[str, LinkedAccount],
        failed_tags: Set[str],
    ) -> discord.Embed:
        account_count = sum(len(members) for members in assignments.values())
        unavailable_count = 0
        move_count = 0
        for clan_code, members in assignments.items():
            for member in members:
                profile = profiles.get(member.player_tag)
                if member.player_tag in failed_tags or profile is None:
                    unavailable_count += 1
                elif profile.clan_code != clan_code:
                    move_count += 1

        summary_parts: List[str] = []
        if account_count > 1 and move_count:
            verb = "needs" if move_count == 1 else "need"
            summary_parts.append(
                f"{move_count} of your {account_count} accounts still {verb} to move."
            )
        if account_count > 1 and unavailable_count:
            noun = "account" if unavailable_count == 1 else "accounts"
            summary_parts.append(
                f"Current clan details are unavailable for {unavailable_count} {noun}."
            )

        roster_link = f"[Browse every CWL roster]({self._full_rosters_url()})"
        embed = discord.Embed(
            title="Where You’re Playing",
            description=(
                f"{' '.join(summary_parts)}\n\n{roster_link}"
                if summary_parts
                else roster_link
            ),
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        for clan_code in CWL_CLAN_CODES:
            members = assignments.get(clan_code)
            if not members:
                continue
            blocks: List[str] = []
            for member in members:
                player_name = self._safe_member_text(member.player_name)
                profile = profiles.get(member.player_tag)
                if member.player_tag in failed_tags or profile is None:
                    location = "Current clan unavailable"
                elif profile.clan_code == clan_code:
                    location = f"Currently in {clan_code}"
                elif profile.clan_code:
                    current_label = self._safe_member_text(profile.clan_code)
                    location = f"Currently in {current_label} · Move to {clan_code}"
                else:
                    location = f"Not in a clan · Move to {clan_code}"
                blocks.append(f"**{player_name}** (`{member.player_tag}`)\n{location}")
            clan_name = CWL_CLAN_NAMES[clan_code]
            clan_tag = CWL_CLAN_TAGS[clan_code]
            clan_label = f"{clan_name} ({clan_code}) · {clan_tag}"
            clan_link = f"[{clan_label}]({CLAN_LINKS[clan_code]})"
            embed.add_field(
                name="CWL clan",
                value=self._join_account_blocks([clan_link, *blocks]),
                inline=False,
            )
        return embed

    @staticmethod
    def _build_member_channels_embed(
        assignments: Dict[str, List[RosterMember]],
    ) -> discord.Embed:
        embed = discord.Embed(
            title="Channels for Your CWL Clans",
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        for clan_code in CWL_CLAN_CODES:
            if clan_code not in assignments:
                continue
            lines: List[str] = []
            info_channel_id = CLAN_CWL_INFO_CHANNELS.get(clan_code)
            war_channel_id = CLAN_WAR_CHANNELS.get(clan_code)
            if info_channel_id:
                lines.append(f"CWL info: <#{info_channel_id}>")
            if war_channel_id:
                lines.append(f"War discussion: <#{war_channel_id}>")
            if lines:
                embed.add_field(name=clan_code, value="\n".join(lines), inline=False)
        return embed

    async def _allow_cwl_placement_lookup(self, interaction: discord.Interaction) -> bool:
        release_status = (
            await self._cwl_placements_release_status(interaction.guild_id)
            if interaction.guild_id is not None
            else None
        )
        if release_status is True:
            return True
        message = (
            "CWL roster details aren't available."
            if release_status is None
            else "The CWL rosters haven’t been announced yet."
        )
        await interaction.response.send_message(message, ephemeral=True)
        return False

    async def show_member_cwl(self, interaction: discord.Interaction) -> None:
        if not await self._allow_cwl_placement_lookup(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        assignments = await self._member_cwl_assignments(
            interaction.guild_id,
            interaction.user.id,
        )
        if assignments is None:
            await interaction.followup.send(
                "Your CWL roster details aren't available.",
                ephemeral=True,
            )
            return
        if not assignments:
            await interaction.followup.send("You aren't on a CWL roster.", ephemeral=True)
            return

        account_map: Dict[str, LinkedAccount] = {}
        for members in assignments.values():
            for member in members:
                account_map.setdefault(
                    member.player_tag,
                    LinkedAccount(
                        player_tag=member.player_tag,
                        player_name=member.player_name,
                        clan_code=member.clan_code,
                        townhall=member.townhall,
                        hero_sum=member.hero_sum,
                    ),
                )
        profiles, failed_tags = await fetch_account_profiles(
            list(account_map.values()),
            self.clash_client,
        )
        await interaction.followup.send(
            embed=self._build_member_cwl_embed(assignments, profiles, failed_tags),
            ephemeral=True,
        )

    async def show_member_cwl_channels(self, interaction: discord.Interaction) -> None:
        if not await self._allow_cwl_placement_lookup(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        assignments = await self._member_cwl_assignments(
            interaction.guild_id,
            interaction.user.id,
        )
        if assignments is None:
            await interaction.followup.send(
                "Your CWL channels aren't available.",
                ephemeral=True,
            )
            return
        if not assignments:
            await interaction.followup.send("You aren't on a CWL roster.", ephemeral=True)
            return
        await interaction.followup.send(
            embed=self._build_member_channels_embed(assignments),
            ephemeral=True,
        )
