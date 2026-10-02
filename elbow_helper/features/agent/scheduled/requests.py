"""Scheduled request contexts and saved-work orchestration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import time
from typing import Any
from uuid import uuid4

import discord
from discord.ext import commands

from ..models import AgentRequestContext, AgentTurnState
from ..engine.service import AgentService
from ..actions.runner import AgentActionRunner
from ..actions.outcomes import command_reply
from .scope import within_scope
from . import watchers

DeliveryFunction = Callable[..., Awaitable[Any]]
ContextFactory = Callable[["ScheduledMessage", discord.Member], AgentRequestContext]


@dataclass(frozen=True, slots=True)
class ScheduledResult:
    last_result: Any = None
    holding: bool | None = None
    completed: bool = False
    action_run: Mapping[str, Any] | None = None


class ScheduledContextFactory:
    def __init__(
        self,
        *,
        bot: commands.Bot,
        collaborators: Mapping[str, Any],
        timeout_seconds: float,
    ) -> None:
        self.bot = bot
        self.collaborators = dict(collaborators)
        self.timeout_seconds = timeout_seconds

    def __call__(
        self, message: ScheduledMessage, member: discord.Member
    ) -> AgentRequestContext:
        return AgentRequestContext(
            bot=self.bot,
            guild=message.guild,
            member=member,
            source_message=message,
            state=AgentTurnState(source_channels={message.channel.id}),
            attachment_sources=(message,),
            deadline_monotonic=time.monotonic() + self.timeout_seconds,
            **self.collaborators,
        )


@dataclass(slots=True)
class ScheduledMessage:
    guild: Any
    channel: Any
    author: Any
    content: str
    id: int = field(default_factory=lambda: int(uuid4().hex[:15], 16))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    reference: None = None
    mentions: tuple[Any, ...] = ()
    raw_mentions: tuple[int, ...] = ()
    attachments: tuple[Any, ...] = ()
    embeds: tuple[Any, ...] = ()
    archive_reply: bool = False

    async def reply(self, content=None, **kwargs):
        kwargs.pop("mention_author", None)
        return await self.channel.send(content, **kwargs)


async def run_saved_request(
    context: AgentRequestContext,
    rule: Mapping[str, Any],
    *,
    service: AgentService,
    action_runner: AgentActionRunner,
    delivery: DeliveryFunction,
) -> ScheduledResult:
    message = context.source_message
    allowed = rule.get("allowed_actions", [])
    local_context = "Confirmed standing scope: " + json.dumps(
        allowed,
        ensure_ascii=False,
        default=str,
    )
    response = await service.answer(
        question=rule["request"],
        local_context=local_context,
        context=context,
    )
    proposals = tuple(context.state.command_proposals)
    if proposals and within_scope(proposals, allowed):
        run_id = await action_runner.submit(
            context,
            proposals,
            confirmer_id=context.member.id,
        )
        run = await action_runner.wait_run(run_id)
        if context.state.command_outcomes or context.state.attachments:
            output_context = replace(
                context,
                state=replace(context.state, command_proposals=[]),
            )
            await delivery(
                message,
                (
                    command_reply(context.state.command_outcomes)
                    if context.state.command_outcomes
                    else ""
                ),
                None,
                context.state.attachments,
                context=output_context,
            )
        return ScheduledResult(action_run=run)
    await delivery(
        message,
        response,
        None,
        context.state.attachments,
        context=context,
    )
    return ScheduledResult()


async def check_watcher(
    context: AgentRequestContext,
    saved: Mapping[str, Any],
) -> ScheduledResult:
    rule = saved["rule"]
    results = await watchers.read_current(context, rule["reads"])
    if results == saved["last_result"]:
        return ScheduledResult(last_result=results, holding=bool(saved["holding"]))
    holds, alert = await watchers.evaluate(context, rule["condition"], results)
    started = holds and not saved["holding"]
    if started:
        await watchers.send_alert(context, alert)
    return ScheduledResult(
        last_result=results,
        holding=holds,
        completed=bool(started and not rule.get("repeat", False)),
    )
