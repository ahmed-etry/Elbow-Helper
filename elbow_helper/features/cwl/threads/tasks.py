"""Thread feature sticky handling plus background task loops."""

from __future__ import annotations

import asyncio
import logging
from datetime import time as dt_time
from datetime import timedelta
from datetime import timezone as dt_timezone

import discord
from discord.ext import tasks

from ..config import STICKY_HTTP_MAX_RATE_LIMIT_RETRY_SECONDS
from ..config import STICKY_HTTP_RETRY_DELAYS_SECONDS
from ..config import STICKY_REFRESH_HOURS
from ..helpers import wait_for_boot_complete


LOGGER = logging.getLogger(__name__)


class CwlThreadTasksMixin:
    async def create_sticky_embed(self, thread: discord.Thread) -> discord.Embed:
        """Create sticky embed for a CWL thread."""
        thread_data = self.data["threads"].get(str(thread.id), {})
        clan_name = thread_data.get("clan_name", "Unknown Clan")
        cc_status = thread_data.get("cc_status", {})
        war_info = await self._get_war_snapshot(clan_name)

        embed = discord.Embed(
            title=f"CWL Status — {clan_name}",
            description="Check this post for round updates and Clan Castle tracking.",
            color=discord.Color.gold(),
            timestamp=self._utc_now(),
        )

        # CC Status section with visual indicators
        cc_text = ""
        for day in range(1, 8):
            status = cc_status.get(str(day), "empty")
            if status == "filled":
                cc_text += f"**Day {day}:** ✅ Filled\n"
            elif status == "empty":
                cc_text += f"**Day {day}:** ❌ Empty\n"
            elif status == "partial":
                cc_text += f"**Day {day}:** ⚠️ Partial\n"

        embed.add_field(
            name="Clan Castle Status",
            value=cc_text or "All Clan Castles are empty",
            inline=False,
        )

        # Quick actions
        embed.add_field(
            name="Update Clan Castles",
            value="Use `/cwl cc` to update Clan Castle status.",
            inline=False,
        )

        if war_info:
            lines = []
            if war_info.get("state") == "preparation":
                lines.append(f"• Prep ends in {war_info['time_left']}")
            else:
                lines.append(f"• Attacks used: {war_info['used']}/{war_info['total']}")
                if war_info.get("show_missing") and war_info.get("missing"):
                    missing_txt = ", ".join(war_info["missing"])
                    lines.append(f"• ⚠️ Missing: {missing_txt}")
                lines.append(f"• Time left: {war_info['time_left']}")
                if war_info.get("next_prep"):
                    prep_end_ts = war_info.get("next_prep_end_ts")
                    if prep_end_ts:
                        lines.append(
                            f"• Next round: {war_info['next_prep']} (prep ends <t:{prep_end_ts}:R>)"
                        )
                    else:
                        lines.append(f"• Next round: {war_info['next_prep']} (prep)")
            embed.add_field(
                name="Current Round",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="Current Round",
                value="No active war data available.",
                inline=False,
            )

        embed.set_footer(text="Last updated")
        return embed


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


    @tasks.loop(minutes=1)
    async def check_sticky_reposition(self) -> None:
        """Check if sticky messages need to be repositioned after conversation ends."""
        try:
            current_time = self._utc_now()

            for thread_id in list(self.data.get("threads", {}).keys()):
                try:
                    last_message_time = self.last_message_times.get(thread_id)
                    # Skip threads with no recorded activity timestamp yet.
                    if not last_message_time:
                        continue

                    conversation_active = self.conversation_active.get(thread_id, False)
                    sticky_repositioned = self.sticky_repositioned.get(thread_id, False)

                    # Repost sticky once conversation has gone quiet.
                    time_since_last_message = current_time - last_message_time
                    if (
                        conversation_active
                        and not sticky_repositioned
                        and time_since_last_message >= timedelta(seconds=10)
                    ):
                        thread = self.bot.get_channel(int(thread_id))
                        if thread and isinstance(thread, discord.Thread):
                            await self.update_thread_sticky(thread)
                            self.conversation_active[thread_id] = False
                            self.sticky_repositioned[thread_id] = True

                except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError) as e:
                    LOGGER.warning("Error checking sticky reposition for thread %s: %s", thread_id, e)
        except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError) as e:
            LOGGER.exception("Error in check_sticky_reposition task: %s", e)


    @check_sticky_reposition.before_loop
    async def before_check_sticky_reposition(self) -> None:
        await wait_for_boot_complete(self.bot)


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


    @tasks.loop(hours=STICKY_REFRESH_HOURS)
    async def refresh_sticky_status(self) -> None:
        """Refresh sticky embeds if they have not been updated recently."""
        await wait_for_boot_complete(self.bot)
        # Only refresh during CWL window (days 1-11) to avoid unnecessary updates.
        if not self._is_cwl_window():
            return
        await self._refresh_all_sticky_if_stale()


    async def _refresh_all_sticky_if_stale(self) -> None:
        """Update sticky embeds when they are older than the refresh threshold."""
        now = self._utc_now()
        refresh_after = timedelta(hours=STICKY_REFRESH_HOURS)
        for thread_id, thread_data in list(self.data.get("threads", {}).items()):
            last_updated = self._parse_iso_timestamp(thread_data.get("sticky_last_updated"))
            if last_updated and now - last_updated < refresh_after:
                continue
            try:
                thread_obj = await self._resolve_registered_thread(str(thread_id))
                if thread_obj:
                    was_archived = getattr(thread_obj, "archived", False)
                    try:
                        if was_archived:
                            if not await self._set_registered_thread_archived(
                                thread_obj,
                                archived=False,
                                reason="Update the registered CWL status post",
                            ):
                                continue
                        await self.update_thread_sticky(thread_obj, force=True, move_to_bottom=False)
                    finally:
                        if was_archived:
                            await self._set_registered_thread_archived(
                                thread_obj,
                                archived=True,
                                reason="Restore the CWL thread's archive state after the status update",
                            )
            except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError) as e:
                LOGGER.warning("Error refreshing sticky for thread %s: %s", thread_id, e)


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

                            await self.clear_thread_messages_except_first(thread_obj)

                            thread_data["cc_status"] = {}
                            thread_data["last_activity"] = self._utc_now_iso()

                            await self.update_thread_sticky(thread_obj, force=True)

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
                LOGGER.warning(
                    "Missing permissions deleting stale sticky for %s (%s): message=%s",
                    thread.name,
                    thread.id,
                    stale_id,
                )
            except discord.HTTPException:
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


    async def update_thread_sticky(
        self, thread: discord.Thread, force: bool = False, move_to_bottom: bool = True
    ) -> None:
        """Update sticky message for a specific thread."""
        try:
            thread_id = str(thread.id)
            thread_data = self.data.get("threads", {}).get(thread_id)
            if not isinstance(thread_data, dict):
                return
            lock = self._get_sticky_lock(thread_id)
            async with lock:
                last_updated = self._parse_iso_timestamp(thread_data.get("sticky_last_updated"))
                now = self._utc_now()
                if not force and last_updated and not move_to_bottom:
                    if now - last_updated < timedelta(minutes=2):
                        return

                old_sticky_id = thread_data.get("sticky_message_id")
                embed = await self.create_sticky_embed(thread)
                old_sticky = None
                if old_sticky_id:
                    try:
                        completed, old_sticky = await self._run_sticky_http_operation(
                            thread,
                            "fetch previous sticky",
                            lambda: thread.fetch_message(old_sticky_id),
                        )
                        if not completed:
                            return
                    except discord.NotFound:
                        old_sticky = None

                if old_sticky and not move_to_bottom:
                    # Update in place to avoid creating a new unread message.
                    try:
                        completed, _ = await self._run_sticky_http_operation(
                            thread,
                            "edit sticky",
                            lambda: old_sticky.edit(embed=embed),
                        )
                    except discord.NotFound:
                        old_sticky = None
                    else:
                        if not completed:
                            return
                        thread_data["sticky_last_updated"] = now.isoformat()
                        await self._cleanup_stale_sticky_messages(
                            thread,
                            thread_data,
                            current_sticky_id=old_sticky.id,
                        )
                        self.save_data()
                        return

                completed, new_sticky = await self._run_sticky_http_operation(
                    thread,
                    "send sticky",
                    lambda: thread.send(embed=embed),
                )
                if not completed or new_sticky is None:
                    return
                pending_stale_sticky_ids = self._get_pending_stale_sticky_ids(
                    thread_data,
                    current_sticky_id=new_sticky.id,
                )
                if old_sticky and old_sticky.id not in pending_stale_sticky_ids:
                    pending_stale_sticky_ids.append(old_sticky.id)
                thread_data["sticky_message_id"] = new_sticky.id
                thread_data["sticky_last_updated"] = now.isoformat()
                self._set_pending_stale_sticky_ids(thread_data, pending_stale_sticky_ids)
                self.save_data()

                if await self._cleanup_stale_sticky_messages(
                    thread,
                    thread_data,
                    current_sticky_id=new_sticky.id,
                ):
                    self.save_data()

        except discord.Forbidden:
            LOGGER.warning("Missing permissions updating sticky message for %s (%s)", thread.name, thread.id)
        except discord.NotFound:
            LOGGER.warning("Sticky update skipped for %s (%s): thread or message no longer exists", thread.name, thread.id)
        except discord.HTTPException:
            LOGGER.exception(
                "Unexpected Discord HTTP error updating sticky message for %s (%s)",
                thread.name,
                thread.id,
            )
        except RuntimeError as e:
            LOGGER.exception("Runtime error updating sticky message for %s (%s): %s", thread.name, thread.id, e)
