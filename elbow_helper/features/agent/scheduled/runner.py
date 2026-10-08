"""Pick due standing rules and run them within member-confirmed limits."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any
from uuid import uuid4

import discord

from ..access import AgentAccessLost, require_access, require_evidence_access
from ..engine.service import AgentUnavailableError
from ..discord_actions.safety import check_post_access
from ..discord_actions.direct_messages import deliver_dms
from ..discord_actions.messages import post_mentions
from ..text import chunk_response
from ..wording import (
    ACTION_STANDING_ACCESS_PAUSED,
    ACTION_STANDING_INTERRUPTED,
    ACTION_STANDING_REQUEST_NAME,
    ACTION_STANDING_RUN_FAILED,
    ACTION_STANDING_WATCHER_NAME,
    ACTION_STANDING_AI_UNAVAILABLE, ACTION_STANDING_REMINDER_NAME, ACTION_STANDING_DM_PAUSED,
)
from .time_rules import next_occurrences
from .requests import (
    ScheduledMessage,
    ContextFactory,
    DeliveryFunction,
    run_saved_request,
    check_watcher, StandingDMUnavailable, ScheduledResult,
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
    ) -> None:
        self.bot = bot
        self.repository = repository
        self.guild_id = guild_id
        self.service = service
        self.delivery = delivery
        self.action_runner = action_runner
        self.context_factory = context_factory
        self._tasks: set[asyncio.Task] = set()
        self._member_locks: dict[int, asyncio.Lock] = {}

    def start(self) -> None:
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
            except Exception:
                LOGGER.exception("Agent standing poll failed")
            await asyncio.sleep(POLL_SECONDS)

    async def tick(self) -> None:
        for kind in ("request", "watcher"):
            due = await asyncio.to_thread(
                self.repository.due_standing,
                kind=kind,
                guild_id=self.guild_id,
                limit=MAX_DUE_PER_KIND,
            )
            for rule in due:
                selected_kind = rule["rule"].get("kind", kind)
                identifier = self._identifier(selected_kind, rule)
                owner = uuid4().hex
                claimed = await asyncio.to_thread(
                    self.repository.claim_standing,
                    kind=selected_kind,
                    identifier=identifier,
                    version=rule["version"],
                    owner=owner,
                )
                if claimed:
                    self._track(asyncio.create_task(self._execute(selected_kind, rule, owner)))

    @staticmethod
    def _identifier(kind: str, rule: dict[str, Any]) -> str:
        return rule["request_id" if kind != "watcher" else "watcher_id"]

    @staticmethod
    def _name(kind: str) -> str:
        return (
            ACTION_STANDING_REQUEST_NAME
            if kind == "request"
            else ACTION_STANDING_REMINDER_NAME if kind == "reminder"
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
            except (discord.NotFound, discord.Forbidden):
                return None
        return (
            channel
            if getattr(getattr(channel, "guild", None), "id", None) == self.guild_id
            else None
        )

    async def _notify(self, channel, member_id: int, text: str, *, mention: bool = True) -> None:
        guild = self.bot.get_guild(self.guild_id)
        member = guild.get_member(member_id) if guild is not None else None
        try:
            await channel.send(
                f"<@{member_id}> {text}" if mention else text,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    roles=False,
                    users=[member] if mention and member is not None else [],
                ),
            )
        except (discord.DiscordException, OSError):
            LOGGER.exception(
                "Agent standing notice could not be sent: channel=%s", channel.id
            )

    async def _execute(self, kind: str, rule: dict[str, Any], owner: str) -> None:
        try:
            await self._execute_rule(kind, rule, owner)
        except Exception:
            LOGGER.exception("Agent standing run task failed: kind=%s rule=%s",
                             kind, self._identifier(kind, rule))
            try:
                await self._finish(kind, rule, owner, status="paused")
                channel = await self._destination(rule)
                if channel is not None:
                    await self._notify(channel, rule["requester_id"],
                                       ACTION_STANDING_RUN_FAILED.format(kind=self._name(kind)))
            finally:
                await asyncio.to_thread(self.repository.release_standing,
                    kind=kind, identifier=self._identifier(kind, rule), owner=owner)

    async def _execute_rule(self, kind: str, rule: dict[str, Any], owner: str) -> None:
        identifier = self._identifier(kind, rule)
        try:
            channel = await self._destination(rule)
        except (TimeoutError, discord.HTTPException) as error:
            if isinstance(error, discord.HTTPException) and error.status < 500:
                raise
            LOGGER.warning("Agent standing destination unavailable: kind=%s rule=%s",
                           kind, identifier)
            await asyncio.to_thread(self.repository.release_standing,
                kind=kind, identifier=identifier, owner=owner)
            return
        if channel is None:
            LOGGER.warning(
                "Agent standing destination missing: kind=%s rule=%s", kind, identifier
            )
            await self._finish(kind, rule, owner, status="paused")
            return
        guild = self.bot.get_guild(self.guild_id)
        try:
            member = require_access(guild, rule["requester_id"], channel)
            if rule["rule"].get("deliver_to", "channel") == "channel":
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
            member.guild, channel, member, rule["rule"].get("request", rule["rule"].get("text", ""))
        )
        context = self.context_factory(message, member)
        result = None
        failure = None
        try:
            context.state.source_channels.update(rule["rule"].get("detail_sources", ()))
            context.state.required_access.update(rule["rule"].get("detail_access", ()))
            if rule["rule"].get("detail_sources") or rule["rule"].get("detail_access"):
                await require_evidence_access(context)
            if kind == "reminder":
                content = rule["rule"]["text"]
                if rule["rule"].get("dm_member_ids"):
                    outcomes = await deliver_dms(context, rule["rule"]["dm_member_ids"], content)
                    if not any(row["delivered"] for row in outcomes):
                        raise ValueError("No reminder recipient received the message")
                else:
                    mentions = post_mentions(context, rule["rule"], content)
                    for part in chunk_response(content):
                        await channel.send(part, allowed_mentions=mentions)
                result = ScheduledResult()
            elif kind == "request":
                result = await run_saved_request(
                    context,
                    rule["rule"],
                    service=self.service,
                    delivery=self.delivery,
                    action_runner=self.action_runner,
                )
            else:
                result = await check_watcher(context, rule)
        except AgentAccessLost:
            failure = ACTION_STANDING_ACCESS_PAUSED.format(kind=self._name(kind))
        except StandingDMUnavailable:
            failure = ACTION_STANDING_DM_PAUSED.format(kind=self._name(kind))
        except discord.HTTPException:
            failure = (
                ACTION_STANDING_DM_PAUSED
                if rule["rule"].get("deliver_to") == "dm" and kind != "reminder"
                else ACTION_STANDING_RUN_FAILED
            ).format(kind=self._name(kind))
        except AgentUnavailableError:
            LOGGER.exception(
                "Agent standing model unavailable: kind=%s rule=%s", kind, identifier
            )
            await self._finish(kind, rule, owner, status="active", notice_sent=True)
            if not rule.get("notice_sent"):
                await self._notify(channel, member.id,
                    ACTION_STANDING_AI_UNAVAILABLE.format(kind=self._name(kind)), mention=False)
            return
        except Exception:
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
                notice_sent=False,
            )
        else:
            await self._finish(kind, rule, owner, notice_sent=False)

    async def _finish(
        self,
        kind: str,
        rule: dict[str, Any],
        owner: str,
        *,
        status: str | None = None,
        last_result=None,
        holding: bool | None = None,
        notice_sent: bool | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        upcoming = next_occurrences(
            rule["rule"]["schedule"], after=now, count=1, watcher=kind == "watcher"
        )
        next_at = upcoming[0].timestamp() if upcoming else None
        if status == "active" and next_at is None:
            next_at = rule["next_run_at" if kind != "watcher" else "next_check_at"]
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
            notice_sent=notice_sent,
        )
