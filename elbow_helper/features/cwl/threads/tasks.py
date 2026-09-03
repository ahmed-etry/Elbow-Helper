"""Thread feature sticky handling plus background task loops."""

from __future__ import annotations

import asyncio
import logging
from datetime import time as dt_time
from datetime import timezone as dt_timezone

import discord
from discord.ext import tasks

from ..config import STICKY_HTTP_MAX_RATE_LIMIT_RETRY_SECONDS
from ..config import STICKY_HTTP_RETRY_DELAYS_SECONDS
from ..helpers import wait_for_boot_complete


LOGGER = logging.getLogger(__name__)


class CwlThreadTasksMixin:
    async def _resolve_registered_thread(self, thread_id: str) -> discord.Thread | None:
        thread_obj = self.bot.get_channel(int(thread_id))
        if not thread_obj:
            try:
                thread_obj = await self.bot.fetch_channel(int(thread_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        return thread_obj if isinstance(thread_obj, discord.Thread) else None


    async def _set_registered_thread_archived(
        self,
        thread: discord.Thread,
        *,
        archived: bool,
        reason: str,
    ) -> bool:
        if archived:
            if thread.archived:
                return True
            try:
                await thread.edit(archived=True, reason=reason)
                return True
            except (discord.Forbidden, discord.HTTPException) as e:
                LOGGER.warning("Failed archiving registered CWL thread %s: %s", thread.id, e)
                return False

        if not thread.archived and not thread.locked:
            return True
        try:
            await thread.edit(archived=False, locked=False, reason=reason)
            return True
        except (discord.Forbidden, discord.HTTPException) as e:
            LOGGER.warning("Failed unarchiving registered CWL thread %s: %s", thread.id, e)
            return False


    async def clear_thread_messages_except_first(self, thread: discord.Thread) -> None:
        """Clear all messages in a thread except the first message and protected messages."""
        try:
            messages_to_delete = []
            deleted_count = 0
            current_time = self._utc_now()
            thread_id = str(thread.id)
            thread_data = self.data["threads"].get(thread_id, {})
            sticky_msg_id = thread_data.get("sticky_message_id")

            first_message = None
            async for msg in thread.history(limit=1, oldest_first=True):
                first_message = msg
                break

            if not first_message:
                LOGGER.info("No messages found in thread %s (%s)", thread.name, thread.id)
                return

            async for message in thread.history(limit=None):
                if message.id == first_message.id:
                    continue

                if message.id == sticky_msg_id:
                    LOGGER.debug("Protected sticky message %s in %s", message.id, thread.name)
                    continue

                if message.pinned:
                    LOGGER.debug("Protected pinned message %s in %s", message.id, thread.name)
                    continue

                # Check if message is older than 14 days (Discord's limit for bulk delete)
                message_age = current_time - message.created_at
                if message_age.days >= 14:
                    # Try individual deletion for old messages
                    try:
                        await message.delete()
                        deleted_count += 1
                        LOGGER.debug("Deleted old message %s in %s", message.id, thread.name)
                        await asyncio.sleep(1)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                        LOGGER.warning("Error deleting old message %s in %s: %s", message.id, thread.name, e)
                else:
                    messages_to_delete.append(message)

                if len(messages_to_delete) >= 100:
                    batch = messages_to_delete[:100]
                    messages_to_delete = messages_to_delete[100:]
                    try:
                        await thread.delete_messages(batch)
                        deleted_count += len(batch)
                        LOGGER.debug("Bulk deleted %s messages in %s", len(batch), thread.name)
                        await asyncio.sleep(5)
                    except discord.HTTPException as http_e:
                        if http_e.code == 50034:
                            LOGGER.info("Bulk delete hit age limit in %s; retrying individually.", thread.name)
                            for msg in batch:
                                try:
                                    await msg.delete()
                                    deleted_count += 1
                                    await asyncio.sleep(1)
                                except (discord.NotFound, discord.HTTPException):
                                    pass
                        else:
                            LOGGER.warning("HTTP error during bulk delete in %s: %s", thread.name, http_e)
                            break
                    except discord.HTTPException as e:
                        LOGGER.warning("Error during bulk delete in %s: %s", thread.name, e)
                        break

            if messages_to_delete:
                while messages_to_delete:
                    batch = messages_to_delete[:100]
                    messages_to_delete = messages_to_delete[100:]
                    try:
                        await thread.delete_messages(batch)
                        deleted_count += len(batch)
                        LOGGER.debug("Bulk deleted %s messages in %s", len(batch), thread.name)
                        await asyncio.sleep(5)
                    except discord.HTTPException as http_e:
                        if http_e.code == 50034:
                            LOGGER.info("Bulk delete hit age limit in %s; retrying individually.", thread.name)
                            for msg in batch:
                                try:
                                    await msg.delete()
                                    deleted_count += 1
                                    await asyncio.sleep(1)
                                except (discord.NotFound, discord.HTTPException):
                                    pass
                        else:
                            LOGGER.warning("HTTP error during bulk delete in %s: %s", thread.name, http_e)
                            break
                    except discord.HTTPException as e:
                        LOGGER.warning("Error during bulk delete in %s: %s", thread.name, e)
                        break

                LOGGER.info("Cleared %s messages in %s (except first message)", deleted_count, thread.name)
            else:
                LOGGER.info("No messages to clear in %s (only first message remains)", thread.name)

        except (discord.Forbidden, discord.HTTPException, RuntimeError) as e:
            LOGGER.exception("Error clearing messages in %s (%s): %s", thread.name, thread.id, e)


    async def _open_registered_cwl_threads_for_season(self) -> None:
        if not self._should_keep_registered_cwl_threads_open():
            return

        for thread_id in list(self.data.get("threads", {}).keys()):
            try:
                thread_obj = await self._resolve_registered_thread(str(thread_id))
                if thread_obj is None:
                    continue
                await self._set_registered_thread_archived(
                    thread_obj,
                    archived=False,
                    reason="Open registered CWL discussion thread for the season",
                )
            except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError) as e:
                LOGGER.warning("Error maintaining visibility for registered CWL thread %s: %s", thread_id, e)


    @tasks.loop(time=dt_time(hour=0, minute=0, tzinfo=dt_timezone.utc))
    async def maintain_registered_thread_visibility(self) -> None:
        """Keep registered CWL discussion threads open during the seasonal discussion window."""
        await self._open_registered_cwl_threads_for_season()


    @maintain_registered_thread_visibility.before_loop
    async def before_maintain_registered_thread_visibility(self) -> None:
        await wait_for_boot_complete(self.bot)
        await self._open_registered_cwl_threads_for_season()


    @tasks.loop(hours=24)
    async def auto_reset_cwl(self) -> None:
        """Automatically reset all CC status and clear messages once per month on or after the 13th UTC."""
        try:
            month_key = self._current_auto_reset_month()
            if not month_key:
                return

            settings = self.data.setdefault("settings", {})
            if settings.get("auto_reset_last_ym") == month_key:
                LOGGER.info("Auto-reset already completed for month=%s; skipping.", month_key)
                return

            LOGGER.info("Auto-resetting CWL status and clearing messages for month=%s", month_key)
            all_success = True

            for thread_id, _ in list(self.data.get("threads", {}).items()):
                thread_data = self.data.get("threads", {}).get(thread_id)
                if not isinstance(thread_data, dict):
                    all_success = False
                    LOGGER.warning("Auto-reset: invalid thread data for %s; skipping", thread_id)
                    continue
                try:
                    thread_obj = await self._resolve_registered_thread(str(thread_id))
                    if thread_obj:
                        clan_name = thread_data.get("clan_name", "Unknown Clan")
                        opened_for_reset = False
                        try:
                            if not await self._set_registered_thread_archived(
                                thread_obj,
                                archived=False,
                                reason="Reset registered CWL thread for the new cycle",
                            ):
                                all_success = False
                                continue
                            opened_for_reset = True

                            board_removed = await self._remove_thread_status_board(
                                str(thread_id),
                                thread_data,
                            )
                            if not board_removed:
                                all_success = False
                            await self.clear_thread_messages_except_first(thread_obj)

                            thread_data["cc_status"] = {}
                            thread_data["cc_statuses"] = {}
                            thread_data.pop("active_prep", None)
                            thread_data["last_activity"] = self._utc_now_iso()

                            LOGGER.info("Auto-reset and message clear completed for %s", clan_name)
                        finally:
                            if opened_for_reset and not await self._set_registered_thread_archived(
                                thread_obj,
                                archived=True,
                                reason="Archive registered CWL thread after monthly reset",
                            ):
                                all_success = False
                    else:
                        # Keep stored data consistent even if thread lookup fails.
                        thread_data["cc_status"] = {}
                        thread_data["cc_statuses"] = {}
                        thread_data.pop("active_prep", None)
                        thread_data["last_activity"] = self._utc_now_iso()
                        all_success = False
                        LOGGER.warning("Auto-reset: thread %s not found; status cleared in data only", thread_id)
                except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError) as e:
                    all_success = False
                    LOGGER.warning("Error auto-resetting thread %s: %s", thread_id, e)

            if all_success:
                settings["auto_reset_last_ym"] = month_key
                LOGGER.info("Auto-reset and message clear completed for all clans")
            else:
                LOGGER.warning(
                    "Auto-reset completed with issues for month=%s; guard not committed so it can be retried.",
                    month_key,
                )
            self.save_data()

        except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError) as e:
            LOGGER.exception("Error in auto_reset_cwl task: %s", e)


    @auto_reset_cwl.before_loop
    async def before_auto_reset_cwl(self) -> None:
        await wait_for_boot_complete(self.bot)


    @staticmethod
    def _is_transient_sticky_http_error(exc: discord.HTTPException) -> bool:
        status = getattr(exc, "status", None)
        if status in {429, 500, 502, 503, 504}:
            return True

        detail = str(exc).lower()
        transient_markers = (
            "upstream connect error",
            "disconnect/reset before headers",
            "connection reset",
            "connection timeout",
            "timed out",
            "timeout",
        )
        return any(marker in detail for marker in transient_markers)


    @staticmethod
    def _sticky_http_retry_seconds(exc: discord.HTTPException, attempt: int) -> float:
        if getattr(exc, "status", None) == 429:
            retry_after = getattr(exc, "retry_after", None)
            if isinstance(retry_after, (int, float)) and retry_after > 0:
                return min(float(retry_after), STICKY_HTTP_MAX_RATE_LIMIT_RETRY_SECONDS)
        return STICKY_HTTP_RETRY_DELAYS_SECONDS[attempt]


    @staticmethod
    def _get_pending_stale_sticky_ids(
        thread_data: dict[str, object],
        *,
        current_sticky_id: int | None = None,
    ) -> list[int]:
        raw_ids = thread_data.get("stale_sticky_message_ids")
        if not isinstance(raw_ids, list):
            return []

        pending_ids = []
        seen_ids = set()
        for raw_id in raw_ids:
            try:
                message_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if message_id == current_sticky_id or message_id in seen_ids:
                continue
            seen_ids.add(message_id)
            pending_ids.append(message_id)
        return pending_ids


    @staticmethod
    def _set_pending_stale_sticky_ids(thread_data: dict[str, object], pending_ids: list[int]) -> None:
        if pending_ids:
            thread_data["stale_sticky_message_ids"] = pending_ids
        else:
            thread_data.pop("stale_sticky_message_ids", None)


    async def _cleanup_stale_sticky_messages(
        self,
        thread: discord.Thread,
        thread_data: dict[str, object],
        *,
        current_sticky_id: int | None,
    ) -> bool:
        had_field = "stale_sticky_message_ids" in thread_data
        stale_ids = self._get_pending_stale_sticky_ids(
            thread_data,
            current_sticky_id=current_sticky_id,
        )
        if not stale_ids:
            self._set_pending_stale_sticky_ids(thread_data, [])
            return had_field

        remaining_ids = []
        for stale_id in stale_ids:
            try:
                completed, stale_message = await self._run_sticky_http_operation(
                    thread,
                    "fetch stale sticky",
                    lambda stale_id=stale_id: thread.fetch_message(stale_id),
                )
                if not completed or stale_message is None:
                    remaining_ids.append(stale_id)
                    continue
            except discord.NotFound:
                continue

            try:
                completed, _ = await self._run_sticky_http_operation(
                    thread,
                    "delete stale sticky",
                    lambda stale_message=stale_message: stale_message.delete(),
                )
                if not completed:
                    remaining_ids.append(stale_id)
            except discord.NotFound:
                continue
            except discord.Forbidden:
                remaining_ids.append(stale_id)
                LOGGER.warning(
                    "Missing permissions deleting stale sticky for %s (%s): message=%s",
                    thread.name,
                    thread.id,
                    stale_id,
                )
            except discord.HTTPException:
                remaining_ids.append(stale_id)
                LOGGER.exception(
                    "Unexpected Discord HTTP error deleting stale sticky for %s (%s): message=%s",
                    thread.name,
                    thread.id,
                    stale_id,
                )

        self._set_pending_stale_sticky_ids(thread_data, remaining_ids)
        return stale_ids != remaining_ids or had_field != ("stale_sticky_message_ids" in thread_data)


    async def _run_sticky_http_operation(
        self,
        thread: discord.Thread,
        operation: str,
        coro_factory,
    ) -> tuple[bool, object | None]:
        total_attempts = len(STICKY_HTTP_RETRY_DELAYS_SECONDS) + 1
        for attempt in range(1, total_attempts + 1):
            try:
                return True, await coro_factory()
            except (discord.NotFound, discord.Forbidden):
                raise
            except discord.HTTPException as exc:
                if not self._is_transient_sticky_http_error(exc):
                    raise
                if attempt >= total_attempts:
                    status = getattr(exc, "status", "unknown")
                    LOGGER.warning(
                        "Sticky update deferred for %s (%s): Discord API status=%s during %s after %s attempts: %s",
                        thread.name,
                        thread.id,
                        status,
                        operation,
                        total_attempts,
                        exc,
                    )
                    return False, None
                await asyncio.sleep(self._sticky_http_retry_seconds(exc, attempt - 1))
        return False, None
