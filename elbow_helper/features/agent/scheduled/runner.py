"""Pick due standing rules and run them within member-confirmed limits."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import sqlite3
from typing import Any
from uuid import uuid4

import discord

from ..access import AgentAccessLost, require_access
from ..engine.service import AgentUnavailableError
from ..discord_actions.safety import check_post_access
from ..wording import (
    ACTION_STANDING_ACCESS_PAUSED,
    ACTION_STANDING_INTERRUPTED,
    ACTION_STANDING_REQUEST_NAME,
    ACTION_STANDING_RUN_FAILED,
    ACTION_STANDING_WATCHER_NAME,
    FAILURE_MESSAGE,
)
from .time_rules import next_occurrences
from .requests import (
    ScheduledMessage,
    ContextFactory,
    DeliveryFunction,
    run_saved_request,
    check_watcher,
)
from ..engine.service import AgentService
from ..actions.runner import AgentActionRunner
from ..actions.store import AgentActionRepository
from discord.ext import commands

LOGGER = logging.getLogger(__name__)
POLL_SECONDS = 30
MAX_DUE_PER_KIND = 20


class ScheduledRunner:
    """Claim due rules and keep their leases until reporting finishes."""

    def __init__(
        self,
        *,
        bot: commands.Bot,
        repository: AgentActionRepository,
        guild_id: int,
        service: AgentService,
        delivery: DeliveryFunction,
        action_runner: AgentActionRunner,
        context_factory: ContextFactory,
        enabled: bool,
    ) -> None:
        self.bot = bot
        self.repository = repository
        self.guild_id = guild_id
        self.service = service
        self.delivery = delivery
        self.action_runner = action_runner
        self.context_factory = context_factory
        self.enabled = enabled
        self._tasks: set[asyncio.Task] = set()
        self._member_locks: dict[int, asyncio.Lock] = {}

    def start(self) -> None:
        if not self.enabled:
            return
        task = asyncio.create_task(self._main())
        self._track(task)

    def cancel(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()

    def _track(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._log_task)

    @staticmethod
    def _log_task(task: asyncio.Task) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            LOGGER.error(
                "Agent standing task failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _main(self) -> None:
        wait_ready = getattr(self.bot, "wait_until_ready", None)
        if callable(wait_ready):
            await wait_ready()
        interrupted = await asyncio.to_thread(
            self.repository.recover_standing_leases,
            guild_id=self.guild_id,
        )
        for rule in interrupted:
            channel = await self._destination(rule)
            if channel is not None:
                await self._notify(
                    channel,
                    rule["requester_id"],
                    ACTION_STANDING_INTERRUPTED.format(kind=self._name(rule["kind"])),
                )
        while True:
            try:
                await self.tick()
            except (OSError, sqlite3.Error, discord.DiscordException):
                LOGGER.exception("Agent standing poll failed")
            await asyncio.sleep(POLL_SECONDS)

    async def tick(self) -> None:
        if not self.enabled:
            return
        for kind in ("request", "watcher"):
            due = await asyncio.to_thread(
                self.repository.due_standing,
                kind=kind,
                guild_id=self.guild_id,
                limit=MAX_DUE_PER_KIND,
            )
            for rule in due:
                identifier = self._identifier(kind, rule)
                owner = uuid4().hex
                claimed = await asyncio.to_thread(
                    self.repository.claim_standing,
                    kind=kind,
                    identifier=identifier,
                    version=rule["version"],
                    owner=owner,
                )
                if claimed:
                    self._track(asyncio.create_task(self._execute(kind, rule, owner)))

    @staticmethod
    def _identifier(kind: str, rule: dict[str, Any]) -> str:
        return rule["request_id" if kind == "request" else "watcher_id"]

    @staticmethod
    def _name(kind: str) -> str:
        return (
            ACTION_STANDING_REQUEST_NAME
            if kind == "request"
            else ACTION_STANDING_WATCHER_NAME
        )

    async def _destination(self, rule: dict[str, Any]):
        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            return None
        channel = guild.get_channel_or_thread(rule["destination_channel_id"])
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(rule["destination_channel_id"])
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        return (
            channel
            if getattr(getattr(channel, "guild", None), "id", None) == self.guild_id
            else None
        )

    async def _notify(self, channel, member_id: int, text: str) -> None:
        guild = self.bot.get_guild(self.guild_id)
        member = guild.get_member(member_id) if guild is not None else None
        try:
            await channel.send(
                f"<@{member_id}> {text}",
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    roles=False,
                    users=[member] if member is not None else [],
                ),
            )
        except discord.DiscordException:
            LOGGER.exception(
                "Agent standing notice could not be sent: channel=%s", channel.id
            )

    async def _execute(self, kind: str, rule: dict[str, Any], owner: str) -> None:
        identifier = self._identifier(kind, rule)
        channel = await self._destination(rule)
        if channel is None:
            LOGGER.warning(
                "Agent standing destination missing: kind=%s rule=%s", kind, identifier
            )
            await self._finish(kind, rule, owner, status="paused")
            return
        guild = self.bot.get_guild(self.guild_id)
        try:
            member = require_access(guild, rule["requester_id"], channel)
            check_post_access(channel, member, guild.me)
        except (AgentAccessLost, ValueError, discord.DiscordException):
            LOGGER.warning(
                "Agent standing access lost: kind=%s rule=%s", kind, identifier
            )
            await self._finish(kind, rule, owner, status="paused")
            await self._notify(
                channel,
                rule["requester_id"],
                ACTION_STANDING_ACCESS_PAUSED.format(kind=self._name(kind)),
            )
            return
        lock = self._member_locks.setdefault(member.id, asyncio.Lock())
        async with lock:
            await self._run_with_access(kind, rule, owner, channel, member)

    async def _run_with_access(self, kind, rule, owner, channel, member):
        identifier = self._identifier(kind, rule)
        message = ScheduledMessage(
            member.guild, channel, member, rule["rule"]["request"]
        )
        context = self.context_factory(message, member)
        result = None
        failure = None
        try:
            if kind == "request":
                result = await run_saved_request(
                    context,
                    rule["rule"],
                    service=self.service,
                    delivery=self.delivery,
                    action_runner=self.action_runner,
                )
            else:
                result = await check_watcher(context, rule)
        except AgentUnavailableError:
            LOGGER.exception(
                "Agent standing model unavailable: kind=%s rule=%s", kind, identifier
            )
            failure = FAILURE_MESSAGE
        except (
            AgentAccessLost,
            ValueError,
            RuntimeError,
            TypeError,
            OSError,
            sqlite3.Error,
            discord.DiscordException,
        ):
            LOGGER.exception(
                "Agent standing run failed: kind=%s rule=%s", kind, identifier
            )
            failure = ACTION_STANDING_RUN_FAILED.format(kind=self._name(kind))
        if failure is not None:
            await self._finish(kind, rule, owner, status="paused")
            await self._notify(channel, member.id, failure)
        elif result is not None:
            await self._finish(
                kind,
                rule,
                owner,
                status="completed" if result.completed else None,
                last_result=result.last_result,
                holding=result.holding,
            )
        else:
            await self._finish(kind, rule, owner)

    async def _finish(
        self,
        kind: str,
        rule: dict[str, Any],
        owner: str,
        *,
        status: str | None = None,
        last_result=None,
        holding: bool | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        upcoming = next_occurrences(
            rule["rule"]["schedule"], after=now, count=1, watcher=kind == "watcher"
        )
        next_at = upcoming[0].timestamp() if upcoming else None
        if status is None:
            status = "active" if next_at is not None else "completed"
        if kind == "watcher":
            if last_result is None:
                last_result = rule.get("last_result")
            if holding is None:
                holding = bool(rule.get("holding"))
        await asyncio.to_thread(
            self.repository.finish_standing,
            kind=kind,
            identifier=self._identifier(kind, rule),
            owner=owner,
            next_at=next_at,
            status=status,
            last_result=last_result,
            holding=holding,
        )
