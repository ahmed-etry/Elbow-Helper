"""Bounded background advancement for durable read-only research jobs."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import discord

from elbow_helper.configuration.roles import CORE

from ..access import AgentAccessLost
from .repository import ResearchJobBusy, ResearchJobConflict, ResearchJobRepository
from ..models import AgentRequestContext
from .execution import advance_discord_research_job


LOGGER = logging.getLogger(__name__)
RESEARCH_POLL_SECONDS = 5.0
RESEARCH_WORK_PER_POLL = 4


class ResearchJobRunner:
    """Advance at most one page per eligible job and polling cycle."""

    def __init__(
        self, *, bot, repository: ResearchJobRepository, message_search,
        guild_id: int, poll_seconds: float = RESEARCH_POLL_SECONDS,
    ):
        if poll_seconds <= 0:
            raise ValueError("Research poll interval must be positive")
        self.bot = bot
        self.repository = repository
        self.message_search = message_search
        self.guild_id = guild_id
        self.poll_seconds = poll_seconds
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._scan_after_job_id: str | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    def wake(self) -> None:
        self._wake.set()

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
                LOGGER.exception("Agent research worker poll failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self.poll_seconds,
                )
            except TimeoutError:
                pass

    async def run_once(self) -> int:
        scopes = await asyncio.to_thread(
            self.repository.claimable_scopes,
            guild_id=self.guild_id, limit=RESEARCH_WORK_PER_POLL,
            after_job_id=self._scan_after_job_id,
        )
        if not scopes and self._scan_after_job_id is not None:
            self._scan_after_job_id = None
            scopes = await asyncio.to_thread(
                self.repository.claimable_scopes,
                guild_id=self.guild_id, limit=RESEARCH_WORK_PER_POLL,
            )
        if scopes:
            self._scan_after_job_id = scopes[-1].job_id
        advanced = 0
        for scope in scopes:
            try:
                context = await self._context(scope)
                if context is None:
                    continue
                result = await advance_discord_research_job(
                    context, scope.job_id, lease_owner=uuid4().hex,
                )
                if "error" not in result:
                    advanced += 1
            except asyncio.CancelledError:
                raise
            except AgentAccessLost:
                LOGGER.info(
                    "Research worker access unavailable: job=%s", scope.job_id,
                )
            except (
                discord.DiscordException, OSError, ResearchJobBusy,
                ResearchJobConflict, RuntimeError, TypeError, ValueError,
                sqlite3.Error,
            ):
                LOGGER.exception(
                    "Research worker page failed: job=%s", scope.job_id,
                )
        return advanced

    async def _context(self, scope) -> AgentRequestContext | None:
        guild = self.bot.get_guild(scope.guild_id)
        if guild is None:
            return None
        member = guild.get_member(scope.requester_id)
        if member is None or not any(role.id in CORE for role in member.roles):
            return None
        channel = guild.get_channel_or_thread(scope.source_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(scope.source_channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                return None
        if getattr(getattr(channel, "guild", None), "id", None) != guild.id:
            return None
        source_message = SimpleNamespace(
            id=scope.conversation_root_id, channel=channel,
            created_at=datetime.now(timezone.utc),
        )
        return AgentRequestContext(
            bot=self.bot, guild=guild, member=member,
            source_message=source_message, account_links=None,
            clan_health=None, message_search=self.message_search,
            thread_discovery=None,
            research_jobs=self.repository,
            conversation_root_id=scope.conversation_root_id,
        )


__all__ = ["ResearchJobRunner"]
