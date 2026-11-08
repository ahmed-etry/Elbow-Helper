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
from elbow_helper.core.background import start_resilient_loop, start_tracked_task
from elbow_helper.infrastructure.discord.channel_ordering import reorder_ticket_channels

from elbow_helper.configuration.channels import EXAMINATION_TICKET_CATEGORY

from .availability import AvailabilityPromptView
from .availability import ExaminationAvailabilityMixin
from .followups import ExaminationFollowupMixin
from .intake import ExaminationPromoIntakeMixin, PromoIntakeView, PromoRouteChangeView
from .panel import ExaminationPanelMixin, ExaminerPanelView
from .routing import ExaminationRoutingMixin
from .routing.fields import TICKET_RENAME
from .state import ExaminationStateStore
from .queries import ExaminationQueries

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
        # The agent reads committed cases and their recorded stages, never unsaved edits.
        self.queries = ExaminationQueries(state_store.saved_cases, history=state_store.stage_history)
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

    async def cog_load(self) -> None:
        await self.state_store.startup()

    def cog_unload(self) -> None:
        state_store = getattr(self, "state_store", None)
        if state_store is not None:
            # Saves already asked for still finish; only retries after a failure stop.
            state_store.close()
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
        return start_tracked_task(
            coroutine,
            name=name,
            tracked=self._background_tasks,
            logger=self.logger,
            label="Examination",
        )

    def _save(self) -> None:
        self.state_store.save()

    def _warn_ticket_reorder_issue(self, key: str, message: str, *args: object) -> None:
        # Escalate blocked reorder conditions to warnings without repeating the same alert every 15 minutes.
        now = time.monotonic()
        last_logged = self._ticket_reorder_issue_log_times.get(key)
        if last_logged is None or (now - last_logged) >= 3600.0:
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

    def _exam_ticket_sort_key(self, channel: discord.TextChannel) -> tuple[int, float]:
        ticket_type = self._get_exam_ticket_type(channel)
        type_index = (EXAM_TICKET_TYPE_ORDER.index(ticket_type)
                      if ticket_type in EXAM_TICKET_TYPE_ORDER else len(EXAM_TICKET_TYPE_ORDER))
        return (type_index, channel.created_at.timestamp())

    @tasks.loop(minutes=15)
    async def organize_tickets(self):
        category = await self._exam_ticket_category()
        if category is None:
            return
        tickets = [channel for channel in category.text_channels if self._is_exam_ticket_channel(channel)]
        order = tuple(channel.id for channel in sorted(tickets, key=self._exam_ticket_sort_key))
        await self.bot.publishing.reorder(
            category.guild.id, (category.id, "text"), order, self._apply_ticket_order,
        )

    async def _exam_ticket_category(self) -> Optional[discord.CategoryChannel]:
        category = self.bot.get_channel(EXAMINATION_TICKET_CATEGORY)
        if not isinstance(category, discord.CategoryChannel):
            try:
                category = await self.bot.fetch_channel(EXAMINATION_TICKET_CATEGORY)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        return category if isinstance(category, discord.CategoryChannel) else None

    async def _apply_ticket_order(self):
        category = await self._exam_ticket_category()
        if category is None:
            self.logger.warning(
                "Examination ticket category %s not found or invalid.",
                EXAMINATION_TICKET_CATEGORY,
            )
            return False
        return await reorder_ticket_channels(
            category,
            self.bot.user,
            is_ticket=self._is_exam_ticket_channel,
            sort_key=self._exam_ticket_sort_key,
            reason="Organize exam tickets",
            logger=self.logger,
            warn=self._warn_ticket_reorder_issue,
            clear=self._clear_ticket_reorder_issue,
            # Channel position edits hit a heavily rate-limited Discord route,
            # so pace them and carry on past a channel the bot may not edit.
            pause_seconds=1.0,
            skip_forbidden=True,
        )


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
