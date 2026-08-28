"""Queue board rendering, reminders, and queue maintenance."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from discord.ext import tasks
from elbow_helper.configuration.channels import TRANSFER_REQUESTS
from elbow_helper.configuration.clans import CLAN_CWL_HELPER_ROLE_IDS
from elbow_helper.configuration.clans import CLAN_EMOJIS
from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.configuration.roles import ELDER_ROLE_ID
from elbow_helper.configuration.roles import LEAD
from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX
from elbow_helper.configuration.style import DEFAULT_THUMBNAIL_URL

from .config import CLAN_TRANSFER_QUEUES
from .config import CWL_SEASON_TRANSFER_CLANS
from .config import REQUEST_TTL_HOURS
from .state import default_clan_state
from .state import save_state
from .views import ClanTransfersView


class ClanTransferQueueMixin:

    def _save_state(self) -> None:
        save_state(self.state)

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _parse_timestamp(self, value: str | None) -> datetime:
        try:
            return datetime.fromisoformat(value) if value else self._now()
        except (TypeError, ValueError):
            return self._now()

    def _prune_expired_pending(self, clan_code: str, *, now: datetime | None = None) -> int:
        clan_state = self._get_clan_state(clan_code)
        pending = list(clan_state.get("pending", []))
        if not pending:
            return 0

        current_time = now or self._now()
        expiry_delta = timedelta(hours=REQUEST_TTL_HOURS)
        active_pending = [
            entry
            for entry in pending
            if (current_time - self._parse_timestamp(entry.get("created_at"))) < expiry_delta
        ]
        expired_count = len(pending) - len(active_pending)
        if not expired_count:
            return 0

        clan_state["pending"] = active_pending
        self._save_state()
        self.logger.info("Expired %s stale transfer request(s) for %s", expired_count, clan_code)
        return expired_count

    async def _expire_stale_requests(
        self,
        clan_code: str,
        *,
        now: datetime | None = None,
        lock_held: bool = False,
    ) -> int:
        if not lock_held:
            async with self.locks[clan_code]:
                return await self._expire_stale_requests(clan_code, now=now, lock_held=True)

        expired_count = self._prune_expired_pending(clan_code, now=now)
        if expired_count:
            await self._delete_ping(clan_code)
        return expired_count

    async def _cleanup_expired_requests(self) -> None:
        now = self._now()
        changed_clans: list[str] = []
        for clan_code in CLAN_TRANSFER_QUEUES:
            async with self.locks[clan_code]:
                expired_count = await self._expire_stale_requests(clan_code, now=now, lock_held=True)
            if expired_count:
                changed_clans.append(clan_code)

        if not changed_clans:
            return

        for clan_code in changed_clans:
            await self.ensure_queue_message(clan_code)
        await self.ensure_global_board()

    def _get_clan_state(self, clan_code: str) -> dict[str, Any]:
        self.state.setdefault("clans", {})
        if clan_code not in self.state["clans"]:
            self.state["clans"][clan_code] = default_clan_state(clan_code)
        return self.state["clans"][clan_code]

    def _mention_with_nickname(self, user_id: int) -> str:
        mention = f"<@{user_id}>"
        guild = self.bot.get_guild(GUILD_ID)
        member = guild.get_member(user_id) if guild else None
        if member is None:
            return mention
        nickname = member.nick or member.name
        return f"{mention} ({nickname})"

    def _is_queue_manager(self, interaction: discord.Interaction, clan_code: str | None = None) -> bool:
        role_ids = {getattr(role, "id", None) for role in getattr(interaction.user, "roles", [])}
        if role_ids & (LEAD | {ELDER_ROLE_ID}):
            return True
        if clan_code in CWL_SEASON_TRANSFER_CLANS:
            helper_role_id = CLAN_CWL_HELPER_ROLE_IDS.get(clan_code)
            return helper_role_id is not None and helper_role_id in role_ids
        return False

    def _get_notify_role_ids(self, clan_code: str) -> list[int]:
        if clan_code in CWL_SEASON_TRANSFER_CLANS:
            helper_role_id = CLAN_CWL_HELPER_ROLE_IDS.get(clan_code)
            if helper_role_id is not None:
                return [helper_role_id]

        clan_state = self._get_clan_state(clan_code)
        role_id = clan_state.get("role_id")
        return [role_id] if role_id is not None else []

    def _suppress_thread_component_reply(self, interaction: discord.Interaction) -> bool:
        return (
            interaction.type == discord.InteractionType.component
            and isinstance(interaction.channel, discord.Thread)
        )

    async def _safe_ephemeral_reply(self, interaction: discord.Interaction, content: str) -> bool:
        try:
            # Component replies in archived threads can bring the thread back into the active list.
            if self._suppress_thread_component_reply(interaction):
                if not interaction.response.is_done():
                    await interaction.response.defer()
                return False
            if interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=True)
            else:
                await interaction.response.send_message(content, ephemeral=True)
            return True
        except (discord.NotFound, discord.HTTPException) as exc:
            self.logger.warning("Failed to send interaction reply: %s", exc)
            return False

    async def _bootstrap(self) -> None:
        await self.bot.wait_until_ready()
        await self._cleanup_expired_requests()
        for clan_code in CLAN_TRANSFER_QUEUES:
            await self.ensure_queue_message(clan_code)
        await self.ensure_global_board()

    async def ensure_queue_message(self, clan_code: str) -> None:
        async with self.locks[clan_code]:
            await self._expire_stale_requests(clan_code, lock_held=True)
            clan_state = self._get_clan_state(clan_code)
            thread = await self._fetch_channel(clan_state["thread_id"])
            if thread is None:
                return
            has_pending = bool(clan_state.get("pending"))
            thread = await self._ensure_thread_active(thread, clan_code)
            if thread is None:
                return

            try:
                embed = self._build_queue_embed(clan_code)
                view = ClanTransfersView(clan_code, enabled=has_pending)
                message_id = clan_state.get("queue_message_id")
                if message_id:
                    try:
                        message = await thread.fetch_message(message_id)
                        await message.edit(embed=embed, view=view)
                        return
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        clan_state["queue_message_id"] = None
                        self._save_state()

                existing = await self._find_existing_queue_message(thread, clan_code)
                if existing is not None:
                    try:
                        await existing.edit(embed=embed, view=view)
                        clan_state["queue_message_id"] = existing.id
                        self._save_state()
                        return
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        self.logger.warning("Failed to edit existing queue message for %s: %s", clan_code, exc)

                try:
                    message = await thread.send(embed=embed, view=view)
                    clan_state["queue_message_id"] = message.id
                    self._save_state()
                except (discord.Forbidden, discord.HTTPException) as exc:
                    self.logger.warning("Failed to send queue message for %s: %s", clan_code, exc)
            finally:
                if not has_pending:
                    await self._archive_thread_if_empty(thread, clan_code)

    async def ensure_global_board(self) -> None:
        async with self.global_lock:
            channel = await self._fetch_channel(TRANSFER_REQUESTS)
            if channel is None:
                return

            embed = self._build_global_embed()
            view = self._build_global_view()
            message_id = self.state.get("global_board_message_id")
            if message_id:
                try:
                    message = await channel.fetch_message(message_id)
                    await message.edit(embed=embed, view=view)
                    self._save_state()
                    return
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                    self.logger.warning("Failed to edit global board %s: %s", message_id, exc)
                    self.state["global_board_message_id"] = None

            try:
                message = await channel.send(embed=embed, view=view)
                self.state["global_board_message_id"] = message.id
                self._save_state()
            except (discord.Forbidden, discord.HTTPException) as exc:
                self.logger.warning("Failed to send global board: %s", exc)

    def _build_queue_embed(self, clan_code: str) -> discord.Embed:
        clan_state = self._get_clan_state(clan_code)
        embed = discord.Embed(
            title=f"{CLAN_EMOJIS.get(clan_code, '📬')} {clan_code} - Clan Transfers",
            color=DEFAULT_EMBED_COLOR_HEX,
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        embed.timestamp = self._now()

        pending = clan_state.get("pending", [])
        if not pending:
            embed.description = "Queue is empty."
            return embed

        lines = []
        for entry in sorted(pending, key=lambda item: item["created_at"], reverse=True):
            created_at = int(self._parse_timestamp(entry.get("created_at")).timestamp())
            user_ref = self._mention_with_nickname(entry["user_id"])
            lines.append(f"• **{user_ref}** — added <t:{created_at}:R>")
        embed.add_field(name="Pending Requests", value="\n".join(lines), inline=False)

        oldest_entry = min(pending, key=lambda item: item["created_at"])
        oldest_ts = int(self._parse_timestamp(oldest_entry.get("created_at")).timestamp())
        embed.add_field(name="Oldest Request", value=f"<t:{oldest_ts}:R>", inline=False)
        embed.set_footer(text="Last updated")
        return embed

    def _build_global_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="Clan Transfer Queues",
            color=DEFAULT_EMBED_COLOR_HEX,
            description="Open a clan queue below. Only clear a queue after the transfers have been handled in-game.",
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)
        embed.timestamp = self._now()

        for clan_code in CLAN_TRANSFER_QUEUES:
            clan_state = self._get_clan_state(clan_code)
            pending = clan_state.get("pending", [])
            if pending:
                oldest_entry = min(pending, key=lambda item: item["created_at"])
                oldest_ts = int(self._parse_timestamp(oldest_entry.get("created_at")).timestamp())
                value = f"**{len(pending)} pending**\nOldest: **<t:{oldest_ts}:R>**"
            else:
                value = "Queue is empty."
            embed.add_field(
                name=f"**{CLAN_EMOJIS.get(clan_code, '📌')} {clan_code}**",
                value=value,
                inline=False,
            )

        embed.set_footer(text="Last updated")
        return embed

    def _build_global_view(self) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        for clan_code, queue in CLAN_TRANSFER_QUEUES.items():
            button = discord.ui.Button(
                label=f"{CLAN_EMOJIS.get(clan_code, '')} {clan_code} queue",
                style=discord.ButtonStyle.link,
                url=f"https://discord.com/channels/{GUILD_ID}/{queue['thread_id']}",
            )
            view.add_item(button)
        return view

    async def _fetch_channel(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            self.logger.warning("Failed to fetch channel %s: %s", channel_id, exc)
            return None

    async def _set_thread_archived(
        self,
        channel: discord.Thread,
        *,
        archived: bool,
        reason: str,
    ) -> discord.Thread | None:
        # discord.py returns a fresh Thread from edit; callers must preserve it.
        if archived:
            if channel.archived:
                return channel
            try:
                return await channel.edit(archived=True, reason=reason)
            except discord.Forbidden:
                self.logger.warning("Cannot archive queue thread %s (missing permissions).", channel.id)
                return None
            except discord.HTTPException as exc:
                self.logger.warning("Failed to archive queue thread %s: %s", channel.id, exc)
                return None

        if not channel.archived and not channel.locked:
            return channel
        try:
            return await channel.edit(archived=False, locked=False, reason=reason)
        except discord.Forbidden:
            self.logger.warning("Cannot unarchive queue thread %s (missing permissions).", channel.id)
            return None
        except discord.HTTPException as exc:
            self.logger.warning("Failed to unarchive queue thread %s: %s", channel.id, exc)
            return None

    async def _ensure_thread_active(self, channel, clan_code: str):
        if not isinstance(channel, discord.Thread):
            return channel
        return await self._set_thread_archived(
            channel,
            archived=False,
            reason=f"Updated the {clan_code} transfer queue",
        )

    async def _archive_thread_if_empty(self, channel, clan_code: str):
        if not isinstance(channel, discord.Thread):
            return channel
        return await self._set_thread_archived(
            channel,
            archived=True,
            reason=f"Cleared the {clan_code} transfer queue",
        )

    def _is_queue_message(self, message: discord.Message, clan_code: str) -> bool:
        if message.author.id != self.bot.user.id:
            return False
        expected_button_ids = {
            f"clan_transfers:clear:{clan_code}",
            f"tq:accept:{clan_code}",
        }
        for row in getattr(message, "components", []):
            for item in getattr(row, "children", []):
                if getattr(item, "custom_id", None) in expected_button_ids:
                    return True
        if not message.embeds:
            return False
        title = str(message.embeds[0].title or "").strip()
        return title.endswith(f"{clan_code} - Clan Transfers") or title.endswith(f"{clan_code} - Transfer Queue")

    async def _find_existing_queue_message(self, thread: discord.abc.Messageable, clan_code: str):
        try:
            async for message in thread.history(limit=200, oldest_first=False):
                if self._is_queue_message(message, clan_code):
                    return message
        except (discord.Forbidden, discord.HTTPException) as exc:
            self.logger.warning("Failed to scan history for %s: %s", clan_code, exc)
        return None

    async def _send_ping(self, clan_code: str) -> None:
        clan_state = self._get_clan_state(clan_code)
        thread = await self._fetch_channel(clan_state["thread_id"])
        if thread is None:
            return
        thread = await self._ensure_thread_active(thread, clan_code)
        if thread is None:
            return

        pending = clan_state.get("pending", [])
        if not pending:
            return

        oldest_entry = min(pending, key=lambda item: item["created_at"])
        oldest_ts = int(self._parse_timestamp(oldest_entry.get("created_at")).timestamp())
        notify_role_ids = self._get_notify_role_ids(clan_code)
        mentions = [f"<@&{role_id}>" for role_id in notify_role_ids]
        mention_text = " ".join(dict.fromkeys(mentions))
        mention_prefix = f"{mention_text} " if mention_text else ""
        content = (
            f"{mention_prefix}New transfer request. **{len(pending)} pending**; "
            f"oldest submitted <t:{oldest_ts}:R>."
        )

        try:
            previous_ping_id = clan_state.get("last_ping_message_id")
            if previous_ping_id:
                try:
                    old_message = await thread.fetch_message(previous_ping_id)
                    await old_message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    self.logger.debug("Failed to delete previous ping for %s", clan_code)
            message = await thread.send(content)
            clan_state["last_ping_message_id"] = message.id
            self._save_state()
        except (discord.Forbidden, discord.HTTPException) as exc:
            self.logger.warning("Failed to send ping for %s: %s", clan_code, exc)

    async def _delete_ping(self, clan_code: str) -> None:
        clan_state = self._get_clan_state(clan_code)
        thread = await self._fetch_channel(clan_state["thread_id"])
        message_id = clan_state.get("last_ping_message_id")
        if thread is None or not message_id:
            clan_state["last_ping_message_id"] = None
            return
        thread = await self._ensure_thread_active(thread, clan_code)
        if thread is None:
            clan_state["last_ping_message_id"] = None
            self._save_state()
            return

        try:
            message = await thread.fetch_message(message_id)
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            self.logger.debug("Failed to delete ping message %s for %s", message_id, clan_code)
        finally:
            clan_state["last_ping_message_id"] = None
            self._save_state()

    async def handle_clear_queue(self, interaction: discord.Interaction, clan_code: str) -> None:
        if not self._is_queue_manager(interaction, clan_code):
            if clan_code in CWL_SEASON_TRANSFER_CLANS:
                message = f"Only leads, elders, and {clan_code} CWL helpers can clear this queue."
            else:
                message = "Only leads and elders can clear a queue."
            await self._safe_ephemeral_reply(interaction, message)
            return

        try:
            if not interaction.response.is_done():
                if self._suppress_thread_component_reply(interaction):
                    await interaction.response.defer()
                else:
                    await interaction.response.defer(ephemeral=True)
        except (discord.InteractionResponded, discord.NotFound):
            self.logger.debug(
                "Transfer clear defer skipped: user_id=%s clan=%s",
                interaction.user.id,
                clan_code,
            )

        async with self.locks[clan_code]:
            clan_state = self._get_clan_state(clan_code)
            cleared_count = len(clan_state.get("pending", []))
            clan_state["pending"] = []
            await self._delete_ping(clan_code)
            self._save_state()

        await self.ensure_queue_message(clan_code)
        await self.ensure_global_board()
        if cleared_count == 1:
            cleared_message = f"Cleared the transfer request for {clan_code}."
        else:
            cleared_message = (
                f"Cleared **{cleared_count}** transfer requests for {clan_code}."
            )
        await self._safe_ephemeral_reply(interaction, cleared_message)

    @tasks.loop(minutes=5)
    async def request_expiry_loop(self) -> None:
        await self.bot.wait_until_ready()
        await self._cleanup_expired_requests()

    @request_expiry_loop.before_loop
    async def before_request_expiry_loop(self) -> None:
        await self.bot.wait_until_ready()
