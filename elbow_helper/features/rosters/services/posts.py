"""Discord post lifecycle and rendering coordination for rosters."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from datetime import timezone as dt_timezone
import logging
import re
from typing import Any

import discord
from discord.ext import commands

from elbow_helper.configuration.clans import CLANS
from elbow_helper.infrastructure.clash import ClashClient

from .accounts import RosterAccountDirectory
from ..repository import RosterRepository
from ..ui.emojis import TownHallEmojiProvider
from ..models import LinkedAccount
from ..models import Roster
from ..models import RosterPost
from .profiles import enrich_accounts
from ..ui.rendering import build_roster_embeds
from ..ui.rendering import roster_page_count
from ..ui.rendering import roster_rows_per_page
from .scheduling import due_window
from .scheduling import next_window
from ..ui.views import RosterMessageView


LOGGER = logging.getLogger(__name__)


def message_page(message: discord.Message, roster_id: int) -> int:
    marker_id = f"roster:page:{roster_id}"
    for row in getattr(message, "components", []):
        for item in getattr(row, "children", []):
            if getattr(item, "custom_id", None) != marker_id:
                continue
            label = str(getattr(item, "label", ""))
            match = re.search(r"(\d+)\s*/", label)
            return max(0, int(match.group(1)) - 1) if match else 0
    return 0


class RosterPostService:
    """Own live roster messages and the data required to render them."""

    def __init__(
        self,
        bot: commands.Bot,
        repository: RosterRepository,
        clash_client: ClashClient,
        accounts: RosterAccountDirectory,
        view_owner: Any,
        emoji_provider: TownHallEmojiProvider | None = None,
    ):
        self._bot = bot
        self._repository = repository
        self._clash_client = clash_client
        self._accounts = accounts
        self._view_owner = view_owner
        self._emojis = emoji_provider or TownHallEmojiProvider(bot)
        self._emoji_posts_refreshed = False

    async def restore_persistent_views(self) -> None:
        roster_cache: dict[int, Roster] = {}
        page_count_cache: dict[int, int] = {}
        for post in await asyncio.to_thread(self._repository.list_posts):
            roster = roster_cache.get(post.roster_id)
            if roster is None:
                roster = await asyncio.to_thread(
                    self._repository.get_roster,
                    post.roster_id,
                )
                if roster is None:
                    continue
                roster_cache[roster.id] = roster
                members = await asyncio.to_thread(
                    self._repository.list_members,
                    roster.id,
                    roster.active_cycle_id,
                )
                page_count_cache[roster.id] = roster_page_count(len(members))
            self._bot.add_view(
                self.message_view(
                    roster,
                    page=0,
                    page_count=page_count_cache[roster.id],
                ),
                message_id=post.message_id,
            )

    async def refresh_posts_after_emoji_load(self) -> None:
        if self._emoji_posts_refreshed:
            return
        emojis = await self._emojis.get()
        if not emojis.is_complete:
            return
        self._emoji_posts_refreshed = True
        roster_ids = {
            post.roster_id
            for post in await asyncio.to_thread(self._repository.list_posts)
        }
        for roster_id in sorted(roster_ids):
            roster = await asyncio.to_thread(
                self._repository.get_roster,
                roster_id,
            )
            if roster is not None:
                await self.refresh(roster)

    async def remove_deleted_message(self, message_id: int) -> None:
        await asyncio.to_thread(self._repository.remove_post, message_id)

    async def remove_deleted_messages(self, message_ids: set[int]) -> None:
        await asyncio.to_thread(self._repository.remove_posts, message_ids)

    async def remove_deleted_channel(self, channel_id: int) -> None:
        await asyncio.to_thread(
            self._repository.remove_posts_for_channel,
            channel_id,
        )

    async def render(
        self,
        roster: Roster,
        page: int | None = 0,
    ) -> tuple[list[discord.Embed], int, int]:
        layout = await asyncio.to_thread(self._repository.get_layout, roster.id)
        members = await asyncio.to_thread(
            self._repository.list_members,
            roster.id,
            roster.active_cycle_id,
        )
        townhall_emojis = await self._emojis.get()
        rows_per_page = roster_rows_per_page(members, layout, townhall_emojis)
        page_count = roster_page_count(len(members), rows_per_page)
        page = page_count - 1 if page is None else min(max(page, 0), page_count - 1)
        start = page * rows_per_page
        page_members = members[start:start + rows_per_page]
        missing_heroes = [member for member in page_members if member.hero_sum <= 0]
        if missing_heroes:
            profiles = await enrich_accounts(
                [
                    LinkedAccount(
                        player_tag=member.player_tag,
                        player_name=member.player_name,
                        clan_code=member.clan_code,
                        townhall=member.townhall,
                        hero_sum=member.hero_sum,
                    )
                    for member in missing_heroes
                ],
                self._clash_client,
            )
            profile_map = {profile.player_tag: profile for profile in profiles}
            snapshots = {
                profile.player_tag: {
                    "player_name": profile.player_name,
                    "clan_code": profile.clan_code,
                    "townhall": profile.townhall,
                    "hero_sum": profile.hero_sum,
                }
                for profile in profiles
                if profile.hero_sum > 0
            }
            if snapshots:
                await asyncio.to_thread(
                    self._repository.refresh_member_snapshots,
                    roster.id,
                    roster.active_cycle_id,
                    snapshots,
                )
                members = [
                    replace(
                        member,
                        player_name=profile.player_name,
                        clan_code=profile.clan_code,
                        townhall=profile.townhall,
                        hero_sum=profile.hero_sum,
                    )
                    if (profile := profile_map.get(member.player_tag)) is not None
                    and profile.hero_sum > 0
                    else member
                    for member in members
                ]
                members.sort(
                    key=lambda member: (
                        -member.townhall,
                        -member.hero_sum,
                        member.player_name.casefold(),
                        member.player_tag,
                    )
                )

        guild = self._bot.get_guild(roster.guild_id)
        clan_icon_url = None
        if roster.clan_code in CLANS:
            candidate = self._accounts.clan_badge_url(roster.clan_code)
            if isinstance(candidate, str) and candidate:
                clan_icon_url = candidate
        display_names: dict[int, str] = {}
        if guild and layout.show_discord:
            for row in members:
                member = guild.get_member(row.discord_user_id)
                if member:
                    display_names[row.discord_user_id] = member.name

        opens_at = None
        closes_at = None
        now = datetime.now(dt_timezone.utc)
        if roster.one_off_open_ts is not None and roster.one_off_close_ts is not None:
            timed_open = datetime.fromtimestamp(
                roster.one_off_open_ts,
                dt_timezone.utc,
            )
            timed_close = datetime.fromtimestamp(
                roster.one_off_close_ts,
                dt_timezone.utc,
            )
            if now < timed_open:
                opens_at = timed_open
            elif roster.status == "open" and now < timed_close:
                closes_at = timed_close
        elif roster.schedule_enabled:
            if roster.status == "open":
                window = due_window(roster, now)
                if (
                    window is not None
                    and window.cycle_key == roster.last_open_cycle_key
                    and window.closes_at > now
                ):
                    closes_at = window.closes_at
            else:
                window = next_window(roster, now)
                if window is not None:
                    opens_at = window.opens_at

        return (
            build_roster_embeds(
                roster,
                members,
                display_names,
                closes_at,
                opens_at=opens_at,
                page=page,
                family_icon_url=(
                    str(guild.icon.url)
                    if guild is not None and guild.icon is not None
                    else None
                ),
                clan_icon_url=clan_icon_url,
                layout=layout,
                townhall_emojis=townhall_emojis,
                rows_per_page=rows_per_page,
            ),
            page,
            page_count,
        )

    def message_view(
        self,
        roster: Roster,
        *,
        page: int,
        page_count: int,
    ) -> RosterMessageView:
        return RosterMessageView(
            self._view_owner,
            roster.id,
            is_open=roster.status == "open",
            buttons_hidden=roster.buttons_hidden,
            page=page,
            page_count=page_count,
        )

    async def fetch(self, post: RosterPost) -> discord.Message | None:
        channel = self._bot.get_channel(post.channel_id)
        if channel is None:
            try:
                channel = await self._bot.fetch_channel(post.channel_id)
            except (discord.NotFound, discord.Forbidden):
                await self.remove_deleted_message(post.message_id)
                return None
            except discord.HTTPException:
                return None
        if not hasattr(channel, "fetch_message"):
            await self.remove_deleted_message(post.message_id)
            return None
        try:
            return await channel.fetch_message(post.message_id)
        except (discord.NotFound, discord.Forbidden):
            await self.remove_deleted_message(post.message_id)
            return None
        except discord.HTTPException:
            return None

    async def refresh(self, roster: Roster) -> None:
        posts = await asyncio.to_thread(self._repository.list_posts, roster.id)
        for post in posts:
            message = await self.fetch(post)
            if message is None:
                continue
            embeds, page, page_count = await self.render(
                roster,
                message_page(message, roster.id),
            )
            try:
                await message.edit(
                    embeds=embeds,
                    view=self.message_view(
                        roster,
                        page=page,
                        page_count=page_count,
                    ),
                )
            except (discord.NotFound, discord.Forbidden):
                await self.remove_deleted_message(post.message_id)
            except discord.HTTPException:
                LOGGER.warning(
                    "Could not refresh roster post roster_id=%s channel=%s "
                    "message=%s",
                    roster.id,
                    post.channel_id,
                    post.message_id,
                )

    async def prune_stale(self) -> None:
        for post in await asyncio.to_thread(self._repository.list_posts):
            await self.fetch(post)

    async def post_interaction_response(
        self,
        roster: Roster,
        interaction: discord.Interaction,
    ) -> discord.InteractionMessage:
        embeds, page, page_count = await self.render(roster)
        message = await interaction.edit_original_response(
            content=None,
            embeds=embeds,
            view=self.message_view(
                roster,
                page=page,
                page_count=page_count,
            ),
        )
        await self.register(roster, message, page, page_count)
        return message

    async def register(
        self,
        roster: Roster,
        message: discord.Message,
        page: int,
        page_count: int,
    ) -> None:
        await asyncio.to_thread(
            self._repository.add_post,
            roster.id,
            message.channel.id,
            message.id,
        )
        self._bot.add_view(
            self.message_view(
                roster,
                page=page,
                page_count=page_count,
            ),
            message_id=message.id,
        )

    async def disable_all(self, roster: Roster) -> tuple[int, ...]:
        """Remove controls from every known post and return failures."""
        posts = await asyncio.to_thread(self._repository.list_posts, roster.id)
        failed_message_ids: list[int] = []
        for post in posts:
            channel = self._bot.get_channel(post.channel_id)
            if channel is None:
                try:
                    channel = await self._bot.fetch_channel(post.channel_id)
                except discord.NotFound:
                    await self.remove_deleted_message(post.message_id)
                    continue
                except (discord.Forbidden, discord.HTTPException):
                    failed_message_ids.append(post.message_id)
                    continue
            if not hasattr(channel, "fetch_message"):
                failed_message_ids.append(post.message_id)
                continue
            try:
                message = await channel.fetch_message(post.message_id)
            except discord.NotFound:
                await self.remove_deleted_message(post.message_id)
                continue
            except (discord.Forbidden, discord.HTTPException):
                failed_message_ids.append(post.message_id)
                continue
            try:
                await message.edit(view=None)
            except discord.NotFound:
                await self.remove_deleted_message(post.message_id)
            except (discord.Forbidden, discord.HTTPException):
                failed_message_ids.append(post.message_id)
        return tuple(failed_message_ids)
