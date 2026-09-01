"""Examination cog shell and lifecycle wiring."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

import discord
from discord.ext import commands, tasks
from elbow_helper.core.background import start_resilient_loop
from elbow_helper.discord.channel_ordering import move_text_channel_within_category

from elbow_helper.configuration.channels import EXAMINATION_TICKET_CATEGORY

from .availability import AvailabilityPromptView
from .availability import ExaminationAvailabilityMixin
from .followups import ExaminationFollowupMixin
from .intake import ExaminationPromoIntakeMixin, PromoIntakeView, PromoRouteChangeView
from .panel import ExaminationPanelMixin, ExaminerPanelView
from .routing import ExaminationRoutingMixin
from .routing.fields import TICKET_RENAME
from .state import ExaminationStateStore

EXAM_TICKET_TYPE_ORDER = ("elder_promo", "clan_promo")
DEPRECATED_ROUTING_CLEANUP_INTERVAL_MINUTES = 30


class Examination(
    ExaminationPromoIntakeMixin,
    ExaminationAvailabilityMixin,
    ExaminationRoutingMixin,
    ExaminationPanelMixin,
    ExaminationFollowupMixin,
    commands.Cog,
):
    """Routes clan and elder promotion tickets to examiners and leadership."""

    def __init__(
        self,
        bot: commands.Bot,
        state_store: ExaminationStateStore,
    ):
        self.bot = bot
        self.logger = logging.getLogger(__name__)
        self._ticket_reorder_issue_log_times: Dict[str, float] = {}
        self.state_store = state_store
        self.state = state_store.state
        self._followup_task: Optional[asyncio.Task] = None
        self._scan_task: Optional[asyncio.Task] = None
        self.panel_view = ExaminerPanelView(self)
        self._promo_intake_view_registered = False
        self._availability_view_registered = False
        self._availability_prompt_inflight: Set[int] = set()
        self._pending_ticket_retries: Dict[int, int] = {}
        self._pending_ticket_notified: Set[int] = set()
        self._pending_ticket_failed: Set[int] = set()
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._promo_reconcile_lock = asyncio.Lock()
        self._promo_reconcile_last_ts = 0.0
        self._scan_task = asyncio.create_task(self.scan_existing_tickets())
        start_resilient_loop(self.organize_tickets)
        start_resilient_loop(self.cleanup_deprecated_routing_messages)

    def cog_unload(self) -> None:
        if self._followup_task and not self._followup_task.done():
            self._followup_task.cancel()
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
        for task in tuple(self._background_tasks):
            if not task.done():
                task.cancel()
        self.organize_tickets.cancel()
        self.cleanup_deprecated_routing_messages.cancel()

    def _start_background_task(
        self,
        coroutine: Coroutine[Any, Any, None],
        *,
        name: str,
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)

        def finish(completed: asyncio.Task[None]) -> None:
            self._background_tasks.discard(completed)
            try:
                completed.result()
            except asyncio.CancelledError:
                return
            except Exception:
                self.logger.exception("Examination background task failed: %s", name)

        task.add_done_callback(finish)
        return task

    def _save(self) -> None:
        self.state_store.save()

    def _warn_ticket_reorder_issue(self, key: str, message: str, *args: object) -> None:
        # Escalate blocked reorder conditions to warnings without repeating the same alert every 15 minutes.
        now = time.monotonic()
        last_logged = self._ticket_reorder_issue_log_times.get(key, 0.0)
        if (now - last_logged) >= 3600.0:
            self._ticket_reorder_issue_log_times[key] = now
            self.logger.warning(message, *args)
            return
        self.logger.debug(message, *args)

    def _clear_ticket_reorder_issue(self, *keys: str) -> None:
        for key in keys:
            self._ticket_reorder_issue_log_times.pop(key, None)

    def _get_examiner_roster(self) -> Dict[str, Any]:
        return self.state_store.examiner_roster()

    def _get_cases(self) -> Dict[str, Any]:
        return self.state_store.cases()

    def _get_case(self, channel_id: int) -> Optional[Dict[str, Any]]:
        return self.state_store.case(channel_id)

    def _get_deprecated_routing_messages(self) -> list[dict[str, Any]]:
        return self.state_store.deprecated_routing_messages()

    @staticmethod
    def _parse_cleanup_timestamp(value: object) -> Optional[datetime]:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    async def _cleanup_deprecated_routing_messages_once(self) -> None:
        queue = self._get_deprecated_routing_messages()
        if not queue:
            return

        now = datetime.now(timezone.utc)
        pending: list[dict[str, Any]] = []
        changed = False
        routing_channel_cache: dict[int, discord.abc.Messageable] = {}

        for entry in list(queue):
            if not isinstance(entry, dict):
                changed = True
                continue
            delete_at = self._parse_cleanup_timestamp(entry.get("delete_at"))
            if delete_at is None:
                changed = True
                continue
            if delete_at > now:
                pending.append(entry)
                continue

            try:
                channel_id = int(entry.get("channel_id"))
                message_id = int(entry.get("message_id"))
            except (TypeError, ValueError):
                changed = True
                continue

            channel = routing_channel_cache.get(channel_id)
            if channel is None:
                cached = self.bot.get_channel(channel_id)
                if isinstance(cached, (discord.TextChannel, discord.Thread)):
                    channel = cached
                else:
                    try:
                        fetched = await self.bot.fetch_channel(channel_id)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pending.append(entry)
                        continue
                    if not isinstance(fetched, (discord.TextChannel, discord.Thread)):
                        changed = True
                        continue
                    channel = fetched
                routing_channel_cache[channel_id] = channel

            try:
                message = await channel.fetch_message(message_id)
                await message.delete()
                changed = True
            except discord.NotFound:
                changed = True
            except discord.Forbidden:
                pending.append(entry)
                self.logger.warning(
                    "Missing permissions deleting deprecated routing message: channel_id=%s message_id=%s",
                    channel_id,
                    message_id,
                )
            except discord.HTTPException:
                pending.append(entry)
                self.logger.exception(
                    "Deprecated routing message cleanup failed: channel_id=%s message_id=%s",
                    channel_id,
                    message_id,
                )

        if changed or len(pending) != len(queue):
            self.state["deprecated_routing_messages"] = pending
            self._save()

    def _get_exam_ticket_type_from_name(self, name: str) -> Optional[str]:
        channel_name = name or ""
        for candidate_type, label in TICKET_RENAME.items():
            emoji = str(label.get("emoji", ""))
            if emoji and channel_name.startswith(f"{emoji}｜"):
                return candidate_type
        return None

    def _get_exam_ticket_type(self, channel: discord.TextChannel) -> Optional[str]:
        name = getattr(channel, "name", "") or ""
        ticket_type = self._get_exam_ticket_type_from_name(name)
        if ticket_type:
            return ticket_type

        case = self._get_case(channel.id)
        ticket_type = case.get("type") if isinstance(case, dict) else None
        if ticket_type in EXAM_TICKET_TYPE_ORDER:
            return str(ticket_type)
        return None

    def _is_exam_ticket_channel(self, channel: discord.abc.GuildChannel) -> bool:
        if not isinstance(channel, discord.TextChannel):
            return False
        if channel.category_id != EXAMINATION_TICKET_CATEGORY:
            return False

        name = getattr(channel, "name", "") or ""
        if name.startswith("ticket-"):
            return True
        if self._get_exam_ticket_type_from_name(name):
            return True
        case = self._get_case(channel.id)
        return bool(isinstance(case, dict) and case.get("type") in EXAM_TICKET_TYPE_ORDER)

    @tasks.loop(minutes=15)
    async def organize_tickets(self) -> None:
        try:
            category = self.bot.get_channel(EXAMINATION_TICKET_CATEGORY)
            if not isinstance(category, discord.CategoryChannel):
                try:
                    fetched = await self.bot.fetch_channel(EXAMINATION_TICKET_CATEGORY)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    fetched = None
                if isinstance(fetched, discord.CategoryChannel):
                    category = fetched
            if not isinstance(category, discord.CategoryChannel):
                self.logger.warning(
                    "Examination ticket category %s not found or invalid.",
                    EXAMINATION_TICKET_CATEGORY,
                )
                return

            me = category.guild.me
            if me is None and self.bot.user:
                me = category.guild.get_member(self.bot.user.id)
            if me is None and self.bot.user:
                try:
                    me = await category.guild.fetch_member(self.bot.user.id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                    self._warn_ticket_reorder_issue(
                        "member_resolve",
                        "Skipping exam ticket reorder: unable to resolve bot member object: %s",
                        exc,
                    )
                    return
            if me is None:
                self._warn_ticket_reorder_issue(
                    "member_missing",
                    "Skipping exam ticket reorder: bot member object unavailable.",
                )
                return
            self._clear_ticket_reorder_issue("member_resolve", "member_missing")

            ticket_channels = [ch for ch in category.text_channels if self._is_exam_ticket_channel(ch)]
            if not ticket_channels:
                self.logger.debug("No examination ticket channels to organize.")
                return

            type_order = {ticket_type: idx for idx, ticket_type in enumerate(EXAM_TICKET_TYPE_ORDER)}

            def sort_key(channel: discord.TextChannel) -> tuple[int, float]:
                ticket_type = self._get_exam_ticket_type(channel)
                type_index = type_order.get(ticket_type, len(type_order))
                return (type_index, channel.created_at.timestamp())

            tracked_ticket_ids = {ch.id for ch in ticket_channels}
            blocked_ticket_ids: set[int] = set()
            max_passes = max(1, len(ticket_channels) * 2)
            move_attempts = 0
            moved_count = 0
            no_perm_channels: set[str] = set()
            forbidden_channels: list[str] = []
            forbidden_details: list[str] = []
            http_failures: list[tuple[str, int, discord.HTTPException]] = []

            for _ in range(max_passes):
                live_ticket_channels = [
                    ch for ch in category.text_channels if ch.id in tracked_ticket_ids and self._is_exam_ticket_channel(ch)
                ]
                if len(live_ticket_channels) != len(tracked_ticket_ids):
                    self.logger.debug(
                        "Exam ticket set changed during reorder (expected=%s got=%s); stopping pass.",
                        len(tracked_ticket_ids),
                        len(live_ticket_channels),
                    )
                    break

                current_order = sorted(live_ticket_channels, key=lambda ch: ch.position)
                desired_order = sorted(live_ticket_channels, key=sort_key)
                current_ids = [ch.id for ch in current_order]
                desired_ids = [ch.id for ch in desired_order]
                if current_ids == desired_ids:
                    break

                mismatch_indexes = [
                    idx for idx, (current_id, desired_id) in enumerate(zip(current_ids, desired_ids))
                    if current_id != desired_id
                ]
                selected_move: Optional[tuple[int, discord.TextChannel]] = None
                for mismatch_idx in mismatch_indexes:
                    candidate = desired_order[mismatch_idx]
                    if candidate.id in blocked_ticket_ids:
                        continue
                    perms = candidate.permissions_for(me)
                    if perms.manage_channels and perms.view_channel:
                        selected_move = (mismatch_idx, candidate)
                        break
                    blocked_ticket_ids.add(candidate.id)
                    no_perm_channels.add(f"{candidate.name} ({candidate.id})")

                if selected_move is None:
                    break

                target_index, channel_to_move = selected_move
                current_index = current_order.index(channel_to_move)
                desired_pos = current_order[target_index].position
                if channel_to_move.position == desired_pos:
                    continue

                move_attempts += 1
                attempted_edit = False
                try:
                    attempted_edit = True
                    await move_text_channel_within_category(
                        channel_to_move,
                        category,
                        current_order,
                        target_index,
                        reason="Organize exam tickets",
                    )
                    moved_count += 1
                except discord.Forbidden as exc:
                    forbidden_channels.append(f"{channel_to_move.name} ({channel_to_move.id})")
                    ch_perms = channel_to_move.permissions_for(me)
                    cat_perms = category.permissions_for(me)
                    perm_snapshot = (
                        f"ch(view={ch_perms.view_channel}, manage={ch_perms.manage_channels}) "
                        f"cat(view={cat_perms.view_channel}, manage={cat_perms.manage_channels})"
                    )
                    err_text = (getattr(exc, "text", "") or str(exc)).replace("\n", " ").strip()
                    forbidden_details.append(
                        (
                            f"{channel_to_move.name} ({channel_to_move.id}) slot {current_index}->{target_index} "
                            f"pos {channel_to_move.position}->{desired_pos} "
                            f"status={getattr(exc, 'status', 'unknown')} "
                            f"code={getattr(exc, 'code', 'unknown')} "
                            f"perms={perm_snapshot} err={err_text}"
                        )
                    )
                    blocked_ticket_ids.add(channel_to_move.id)
                    continue
                except discord.HTTPException as exc:
                    http_failures.append((channel_to_move.name, channel_to_move.id, exc))
                    break
                finally:
                    # Channel position edits hit a heavily rate-limited Discord route.
                    # Pace reorder attempts so initial cleanup does not burst requests.
                    if attempted_edit:
                        await asyncio.sleep(1.0)
            else:
                self.logger.debug(
                    "Exam ticket reorder reached max passes (%s); stopping to avoid churn.",
                    max_passes,
                )

            if no_perm_channels:
                no_perm_sorted = sorted(no_perm_channels)
                preview = ", ".join(no_perm_sorted[:5])
                suffix = f" (+{len(no_perm_sorted) - 5} more)" if len(no_perm_sorted) > 5 else ""
                self.logger.debug(
                    "Skipped exam ticket reorder for %s channels without manage_channels: %s%s",
                    len(no_perm_sorted),
                    preview,
                    suffix,
                )
            if forbidden_channels:
                preview = ", ".join(forbidden_channels[:5])
                suffix = f" (+{len(forbidden_channels) - 5} more)" if len(forbidden_channels) > 5 else ""
                self.logger.debug(
                    "Exam ticket reorder received forbidden on %s channel edits (moved=%s/%s): %s%s",
                    len(forbidden_channels),
                    moved_count,
                    move_attempts,
                    preview,
                    suffix,
                )
                if forbidden_details:
                    details_preview = " | ".join(forbidden_details[:2])
                    details_suffix = f" (+{len(forbidden_details) - 2} more)" if len(forbidden_details) > 2 else ""
                    self.logger.debug("Exam ticket reorder forbidden details: %s%s", details_preview, details_suffix)
            if http_failures:
                preview = " | ".join(f"{name} ({cid}): {err}" for name, cid, err in http_failures[:3])
                suffix = f" (+{len(http_failures) - 3} more)" if len(http_failures) > 3 else ""
                self.logger.warning(
                    "Exam ticket reorder had %s Discord API edit failures: %s%s",
                    len(http_failures),
                    preview,
                    suffix,
                )
        except (discord.Forbidden, discord.HTTPException, RuntimeError):
            self.logger.exception("Error in examination ticket reordering task")

    @organize_tickets.before_loop
    async def before_organize_tickets(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=DEPRECATED_ROUTING_CLEANUP_INTERVAL_MINUTES)
    async def cleanup_deprecated_routing_messages(self) -> None:
        try:
            await self._cleanup_deprecated_routing_messages_once()
        except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError):
            self.logger.exception("Deprecated routing message cleanup failed")

    @cleanup_deprecated_routing_messages.before_loop
    async def before_cleanup_deprecated_routing_messages(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        if not isinstance(channel, discord.TextChannel):
            return
        if channel.category_id != EXAMINATION_TICKET_CATEGORY:
            return
        for attempt in range(1, 21):
            if attempt > 1:
                await asyncio.sleep(1)
            await self.process_ticket_channel(channel)
            if str(channel.id) in self._get_cases():
                break

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if not isinstance(channel, discord.TextChannel):
            return
        cases = self._get_cases()
        case = cases.pop(str(channel.id), None)
        if not case:
            return
        routing_msg_id = case.get("routing_message_id")
        if routing_msg_id:
            routing_channel = await self._get_routing_channel()
            if routing_channel:
                try:
                    msg = await routing_channel.fetch_message(routing_msg_id)
                    await msg.delete()
                except discord.NotFound:
                    self.logger.debug(
                        "Routing message already missing during ticket cleanup: channel_id=%s message_id=%s",
                        channel.id,
                        routing_msg_id,
                    )
                except discord.Forbidden:
                    self.logger.warning(
                        "Missing permissions deleting routing message during ticket cleanup: channel_id=%s message_id=%s",
                        channel.id,
                        routing_msg_id,
                    )
                except discord.HTTPException:
                    self.logger.exception(
                        "Routing message cleanup failed: channel_id=%s message_id=%s",
                        channel.id,
                        routing_msg_id,
                    )
        self._save()

    async def scan_existing_tickets(self) -> None:
        await self.bot.wait_until_ready()
        try:
            for guild in self.bot.guilds:
                category = discord.utils.get(guild.categories, id=EXAMINATION_TICKET_CATEGORY)
                if not category:
                    continue
                has_exam_tickets = any(
                    isinstance(channel, discord.TextChannel) and self._is_exam_ticket_channel(channel)
                    for channel in category.channels
                )
                for channel in list(category.channels):
                    # Only raw TicketTool channels need startup processing.
                    # Already-renamed exam tickets are sorted by name and should not be re-routed on boot.
                    if isinstance(channel, discord.TextChannel) and channel.name.startswith("ticket-"):
                        await self.process_ticket_channel(channel)
                        await asyncio.sleep(1.5)
                if has_exam_tickets:
                    await self.organize_tickets.coro(self)
            await self._reconcile_promo_route_controls()
        except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError):
            self.logger.exception("Existing exam ticket scan failed")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._followup_task is None:
            self._followup_task = asyncio.create_task(self._followup_loop())
        if not self._availability_view_registered:
            self.bot.add_view(AvailabilityPromptView(self))
            self._availability_view_registered = True
        if not self._promo_intake_view_registered:
            self.bot.add_view(PromoIntakeView(self, register_all=True))
            self.bot.add_view(PromoRouteChangeView(self))
            self._promo_intake_view_registered = True
        try:
            await self._reconcile_promo_route_controls()
        except (discord.Forbidden, discord.HTTPException, RuntimeError, TypeError, ValueError):
            self.logger.exception("Promotion route control reconciliation failed during on_ready")
        try:
            await self._post_panel()
        except (discord.Forbidden, discord.HTTPException, RuntimeError):
            self.logger.exception("Examiner panel initialization failed during on_ready")
