"""Missing-elder board generation and sticky maintenance."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import discord

from elbow_helper.configuration.style import DEFAULT_EMBED_COLOR_HEX
from elbow_helper.configuration.style import DEFAULT_THUMBNAIL_URL

from .config import MISSING_ELDER_RECOVERY_SCAN_LIMIT
from .config import MISSING_ELDER_STICKY_BURIAL_MESSAGES
from .config import MISSING_ELDER_STICKY_REPOST_COOLDOWN_SECONDS
from .state import save_state
from .views import MissingElderBoardView


LOGGER = logging.getLogger(__name__)


class ClanReportingElderMixin:
    """Missing-elder board refresh, rendering, and sticky repost logic."""

    _board_last_repost_at: dict[str, float]
    _board_repost_locks: dict[str, asyncio.Lock]

    async def _refresh_all_elder_lists(self) -> None:
        await self.account_links.wait_until_snapshot_ready()
        for clan_code in self._board_clan_codes():
            await self._update_missing_elder_message(clan_code, reposition_if_buried=False)
            await asyncio.sleep(0.4)

    def _board_clan_codes(self) -> tuple[str, ...]:
        from elbow_helper.configuration.channels import CLAN_LEADERSHIP_CHANNELS

        return tuple(CLAN_LEADERSHIP_CHANNELS.keys())

    async def _update_missing_elder_message(self, clan_code: str, *, reposition_if_buried: bool = False) -> None:
        channel = await self._get_leadership_channel(clan_code)
        if channel is None:
            return

        lock = self._get_board_repost_lock(clan_code)
        async with lock:
            embed = await self._build_missing_elder_embed(clan_code)
            view = MissingElderBoardView(self, clan_code)
            board_messages = self.state.setdefault("missing_elder_messages", {})
            message = None
            message_id = board_messages.get(clan_code)
            if message_id:
                message, fetch_completed = await self._fetch_tracked_missing_elder_message(
                    channel,
                    clan_code,
                    message_id,
                )
                if not fetch_completed:
                    return
            if message is None:
                recovered_message, is_buried, recovery_completed = await self._find_missing_elder_board_in_history(
                    channel,
                    clan_code,
                )
                if not recovery_completed:
                    return
                if recovered_message is not None:
                    board_messages[clan_code] = recovered_message.id
                    save_state(self.state)
                    message = recovered_message
                    if reposition_if_buried and is_buried:
                        await self._repost_missing_elder_message(
                            channel,
                            clan_code,
                            old_message=recovered_message,
                            embed=embed,
                            view=view,
                        )
                        return

            if message is None:
                new_message = await self._run_discord_http_operation(
                    clan_code,
                    "send missing-elder board",
                    lambda: channel.send(embed=embed, view=view),
                )
                if new_message is None:
                    return
                board_messages[clan_code] = new_message.id
                save_state(self.state)
                return

            try:
                await self._run_discord_http_operation(
                    clan_code,
                    "edit missing-elder board",
                    lambda: message.edit(embed=embed, view=view),
                )
            except discord.NotFound as exc:
                LOGGER.warning("Failed to edit missing-elder board for %s: %s", clan_code, exc)
                recovered_message, _, recovery_completed = await self._find_missing_elder_board_in_history(
                    channel,
                    clan_code,
                )
                if not recovery_completed:
                    return
                if recovered_message is not None:
                    board_messages[clan_code] = recovered_message.id
                    save_state(self.state)
                    try:
                        await self._run_discord_http_operation(
                            clan_code,
                            "edit recovered missing-elder board",
                            lambda: recovered_message.edit(embed=embed, view=view),
                        )
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as edit_exc:
                        LOGGER.warning("Failed to edit recovered missing-elder board for %s: %s", clan_code, edit_exc)
                    return
                new_message = await self._run_discord_http_operation(
                    clan_code,
                    "send replacement missing-elder board",
                    lambda: channel.send(embed=embed, view=view),
                )
                if new_message is None:
                    return
                board_messages[clan_code] = new_message.id
                save_state(self.state)
            except (discord.Forbidden, discord.HTTPException) as exc:
                LOGGER.warning("Failed to edit missing-elder board for %s: %s", clan_code, exc)

    def _get_board_repost_lock(self, clan_code: str) -> asyncio.Lock:
        lock = self._board_repost_locks.get(clan_code)
        if lock is None:
            lock = asyncio.Lock()
            self._board_repost_locks[clan_code] = lock
        return lock

    async def _fetch_tracked_missing_elder_message(
        self,
        channel: discord.TextChannel,
        clan_code: str,
        message_id: int | str,
    ) -> tuple[Optional[discord.Message], bool]:
        try:
            parsed_message_id = int(message_id)
            message = await self._run_discord_http_operation(
                clan_code,
                "fetch tracked missing-elder board",
                lambda: channel.fetch_message(parsed_message_id),
            )
            return message, message is not None
        except discord.NotFound:
            return None, True
        except (discord.Forbidden, discord.HTTPException, ValueError, TypeError):
            return None, False

    def _is_missing_elder_board_message(self, message: discord.Message, clan_code: str) -> bool:
        if not message.embeds:
            return False
        title = message.embeds[0].title or ""
        return title.startswith("Missing Elder Rank") and f"— {clan_code}" in title

    async def _find_missing_elder_board_in_history(
        self,
        channel: discord.TextChannel,
        clan_code: str,
    ) -> tuple[Optional[discord.Message], bool, bool]:
        async def scan_history() -> tuple[Optional[discord.Message], bool]:
            newer_count = 0
            async for message in channel.history(
                limit=MISSING_ELDER_RECOVERY_SCAN_LIMIT,
                oldest_first=False,
            ):
                if self._is_missing_elder_board_message(message, clan_code):
                    return message, newer_count >= MISSING_ELDER_STICKY_BURIAL_MESSAGES
                newer_count += 1
            return None, False

        try:
            result = await self._run_discord_http_operation(
                clan_code,
                "scan missing-elder board history",
                scan_history,
            )
            if result is not None:
                message, is_buried = result
                return message, is_buried, True
        except (discord.Forbidden, discord.HTTPException):
            return None, False, False
        return None, False, False

    async def _repost_missing_elder_message(
        self,
        channel: discord.TextChannel,
        clan_code: str,
        *,
        old_message: discord.Message,
        embed: discord.Embed,
        view: MissingElderBoardView,
    ) -> discord.Message:
        board_messages = self.state.setdefault("missing_elder_messages", {})
        new_message = await self._run_discord_http_operation(
            clan_code,
            "send reposted missing-elder board",
            lambda: channel.send(embed=embed, view=view),
        )
        if new_message is None:
            return old_message
        board_messages[clan_code] = new_message.id
        self._board_last_repost_at[clan_code] = time.monotonic()
        save_state(self.state)

        try:
            await self._run_discord_http_operation(
                clan_code,
                "delete old missing-elder board",
                lambda: old_message.delete(),
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            LOGGER.debug("Failed to delete old missing-elder board for %s: %s", clan_code, exc)
        return new_message

    async def _should_repost_missing_elder_message(
        self,
        channel: discord.TextChannel,
        board_message_id: int,
        clan_code: str,
        *,
        trigger_message: discord.Message,
    ) -> bool:
        if trigger_message.id == board_message_id:
            return False

        last_repost_at = self._board_last_repost_at.get(clan_code)
        if (
            last_repost_at is not None
            and time.monotonic() - last_repost_at
            < MISSING_ELDER_STICKY_REPOST_COOLDOWN_SECONDS
        ):
            return False

        async def count_newer_messages() -> int:
            newer_count = 0
            async for _ in channel.history(
                limit=MISSING_ELDER_STICKY_BURIAL_MESSAGES,
                after=discord.Object(id=board_message_id),
                oldest_first=False,
            ):
                newer_count += 1
                if newer_count >= MISSING_ELDER_STICKY_BURIAL_MESSAGES:
                    break
            return newer_count

        try:
            newer_count = await self._run_discord_http_operation(
                clan_code,
                "check missing-elder board burial",
                count_newer_messages,
            )
        except (discord.Forbidden, discord.HTTPException):
            return False
        if newer_count is None:
            return False

        return newer_count >= MISSING_ELDER_STICKY_BURIAL_MESSAGES

    async def _repost_missing_elder_message_from_activity(
        self,
        clan_code: str,
        trigger_message: discord.Message,
    ) -> bool:
        channel = await self._get_leadership_channel(clan_code)
        if channel is None:
            return False

        lock = self._get_board_repost_lock(clan_code)
        async with lock:
            board_messages = self.state.setdefault("missing_elder_messages", {})
            board_message_id = board_messages.get(clan_code)
            old_message = None
            should_repost = False

            if board_message_id:
                if not await self._should_repost_missing_elder_message(
                    channel,
                    int(board_message_id),
                    clan_code,
                    trigger_message=trigger_message,
                ):
                    return False

                old_message, fetch_completed = await self._fetch_tracked_missing_elder_message(
                    channel,
                    clan_code,
                    board_message_id,
                )
                if old_message is None and not fetch_completed:
                    return False
                should_repost = old_message is not None

            if old_message is None:
                recovered_message, is_buried, recovery_completed = await self._find_missing_elder_board_in_history(
                    channel,
                    clan_code,
                )
                if not recovery_completed or recovered_message is None:
                    return False
                board_messages[clan_code] = recovered_message.id
                save_state(self.state)
                old_message = recovered_message
                should_repost = is_buried and trigger_message.id != recovered_message.id

            if not should_repost:
                return False

            embed = await self._build_missing_elder_embed(clan_code)
            view = MissingElderBoardView(self, clan_code)
            await self._repost_missing_elder_message(
                channel,
                clan_code,
                old_message=old_message,
                embed=embed,
                view=view,
            )
            return True

    async def _build_missing_elder_embed(self, clan_code: str) -> discord.Embed:
        rows = self.account_links.get_missing_elder_rows(clan_code)

        title_base = f"Missing Elder Rank — {clan_code}"
        title = title_base if not rows else f"{title_base} ({len(rows)})"

        embed = discord.Embed(
            title=title,
            color=discord.Color(DEFAULT_EMBED_COLOR_HEX),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)

        if not rows:
            embed.description = "No members are missing the Elder rank in-game."
            return embed

        lines = [
            f"- <@{int(row['discord_user_id'])}> - {row['player_name']}"
            for row in rows
        ]
        embed.description = "\n".join(lines)
        return embed
