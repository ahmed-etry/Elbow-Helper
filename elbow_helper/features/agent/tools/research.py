"""Conversation-scoped, durable Discord research-job tools."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import AgentAccessLost, accessible_message_channel, require_evidence_access
from ..reports.base import ArtifactCapacityError, retain_report
from ..models import AgentRequestContext, RegisteredAgentTool
from ..research.execution import advance_discord_research_job
from ..reports.research import DiscordResearchReport
from .discord import (
    HISTORY_PAGE_LIMIT, SEARCH_RESULT_LIMIT,
)
from .research_batches import research_batch_tools


def research_tools() -> tuple[RegisteredAgentTool, ...]:
    definitions = (
        (
            "start_discord_research_job",
            "Queue a bounded, six-hour research job for one currently accessible Discord channel. A read-only worker advances authorized pages without background model work or Discord writes; status and exact evidence remain conversation scoped.",
            {
                "channel_id": {"type": "integer", "minimum": 1},
                "query": {"type": "string", "maxLength": 1024},
                "author_id": {"type": "integer", "minimum": 1},
                "after": {"type": "string", "maxLength": 40},
                "before": {"type": "string", "maxLength": 40},
                "page_size": {
                    "type": "integer", "minimum": 1,
                    "maximum": SEARCH_RESULT_LIMIT,
                },
            },
            ("channel_id",),
            start_discord_research_job,
        ),
        (
            "start_discord_history_job",
            "Queue a bounded, six-hour job that traverses currently available messages in one accessible Discord channel or thread over an explicit period, independent of keyword indexing. A read-only worker rechecks access between pages and performs no model work or Discord writes.",
            {
                "channel_id": {"type": "integer", "minimum": 1},
                "after": {"type": "string", "maxLength": 40},
                "before": {"type": "string", "maxLength": 40},
                "author_id": {"type": "integer", "minimum": 1},
                "page_size": {
                    "type": "integer", "minimum": 1,
                    "maximum": HISTORY_PAGE_LIMIT,
                },
            },
            ("channel_id", "after"),
            start_discord_history_job,
        ),
        (
            "continue_discord_research_job",
            "Request one immediate authorized page of a retained Discord search or channel-history job instead of waiting for its worker. Progress uses the same leases and restart-persistent checkpoints; this performs no model work or Discord write action.",
            {
                "job_id": {"type": "string", "maxLength": 32},
                "expected_version": {"type": "integer", "minimum": 0},
            },
            ("job_id",),
            continue_discord_research_job,
        ),
        (
            "read_discord_research_job",
            "Read status or a bounded page of retained results from a research job in this conversation after rechecking current source access.",
            {
                "job_id": {"type": "string", "maxLength": 32},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            },
            ("job_id",),
            read_discord_research_job,
        ),
        (
            "retain_discord_research_report",
            "Turn a completed or terminal partial Discord research job into a "
            "restart-persistent evidence report with exact messages, scope and "
            "coverage. Continuing, cancelled or failed jobs cannot become reports. "
            "This performs no Discord write action.",
            {"job_id": {"type": "string", "maxLength": 32}},
            ("job_id",),
            retain_discord_research_report,
        ),
        (
            "read_discord_research_report",
            "Read a bounded page from a retained Discord research report after "
            "current source access is rechecked, without rerunning the research job.",
            {
                "report_id": {"type": "string", "maxLength": 32},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            },
            ("report_id",),
            read_discord_research_report,
        ),
        (
            "cancel_discord_research_job",
            "Cancel a research job created by the current requester in this conversation. Cancellation does not expose retained results or affect Discord.",
            {"job_id": {"type": "string", "maxLength": 32}},
            ("job_id",),
            cancel_discord_research_job,
        ),
    )
    return tuple(
        RegisteredAgentTool(
            AgentToolDefinition(
                name=name,
                description=description,
                parameters={
                    "type": "object", "properties": properties,
                    "required": list(required), "additionalProperties": False,
                },
            ),
            handler,
        )
        for name, description, properties, required, handler in definitions
    ) + research_batch_tools()


async def start_discord_research_job(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.research_jobs is None or context.conversation_root_id is None:
        return {"error": "Durable research jobs are not available."}
    channel_id = arguments.get("channel_id")
    if type(channel_id) is not int or channel_id <= 0:
        return {"error": "A valid channel ID is required."}
    if await accessible_message_channel(context, channel_id) is None:
        return {"error": "The asker cannot access that conversation."}
    query = str(arguments.get("query") or "").strip()
    author_id = arguments.get("author_id")
    if author_id is not None and (type(author_id) is not int or author_id <= 0):
        return {"error": "A valid author ID is required."}
    if not query and not any(arguments.get(key) for key in ("author_id", "after", "before")):
        return {"error": "Supply search words, an author, or a date range."}
    try:
        after = _search_date(arguments.get("after"))
        before = _search_date(arguments.get("before"))
        if before is None:
            before = _message_time(context)
        if after is not None and after >= before:
            raise ValueError("Invalid research period")
    except ValueError:
        return {"error": "Use a valid search period whose start is before its end."}
    page_size = arguments.get("page_size", 10)
    if (
        type(page_size) is not int
        or not 1 <= page_size <= SEARCH_RESULT_LIMIT
    ):
        return {
            "error": f"Use a page size from 1 to {SEARCH_RESULT_LIMIT}."
        }
    job = await asyncio.to_thread(
        context.research_jobs.create,
        guild_id=context.guild.id,
        conversation_root_id=context.conversation_root_id,
        requester_id=context.member.id,
        source_channel_id=channel_id,
        query=query,
        author_id=author_id,
        after=after.isoformat() if after is not None else None,
        before=before.isoformat(),
        page_size=page_size,
    )
    try:
        if await accessible_message_channel(context, channel_id) is None:
            raise AgentAccessLost("Research source access changed")
        context.state.source_channels.add(channel_id)
        await require_evidence_access(context)
    except AgentAccessLost:
        await asyncio.to_thread(
            context.research_jobs.cancel,
            job.job_id,
            guild_id=context.guild.id,
            conversation_root_id=context.conversation_root_id,
            requester_id=context.member.id,
        )
        raise
    return job.manifest()


async def start_discord_history_job(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    if context.research_jobs is None or context.conversation_root_id is None:
        return {"error": "Durable research jobs are not available."}
    channel_id = arguments.get("channel_id")
    author_id = arguments.get("author_id")
    if type(channel_id) is not int or channel_id <= 0:
        return {"error": "A valid channel ID is required."}
    if author_id is not None and (type(author_id) is not int or author_id <= 0):
        return {"error": "A valid author ID is required."}
    if await accessible_message_channel(context, channel_id) is None:
        return {"error": "The asker cannot access that conversation."}
    try:
        after = _search_date(arguments.get("after"))
        before = _search_date(arguments.get("before")) or _message_time(context)
        if after is None or after >= before:
            raise ValueError("Invalid research period")
    except ValueError:
        return {"error": "Use a valid history period whose start is before its end."}
    page_size = arguments.get("page_size", 25)
    if (
        type(page_size) is not int
        or not 1 <= page_size <= HISTORY_PAGE_LIMIT
    ):
        return {
            "error": f"Use a page size from 1 to {HISTORY_PAGE_LIMIT}."
        }
    job = await asyncio.to_thread(
        context.research_jobs.create,
        guild_id=context.guild.id,
        conversation_root_id=context.conversation_root_id,
        requester_id=context.member.id,
        source_channel_id=channel_id,
        query="",
        author_id=author_id,
        after=after.isoformat(),
        before=before.isoformat(),
        page_size=page_size,
        kind="history",
    )
    try:
        if await accessible_message_channel(context, channel_id) is None:
            raise AgentAccessLost("Research source access changed")
        context.state.source_channels.add(channel_id)
        await require_evidence_access(context)
    except AgentAccessLost:
        await asyncio.to_thread(
            context.research_jobs.cancel,
            job.job_id,
            guild_id=context.guild.id,
            conversation_root_id=context.conversation_root_id,
            requester_id=context.member.id,
        )
        raise
    return job.manifest()


async def continue_discord_research_job(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    return await advance_discord_research_job(
        context, arguments["job_id"], lease_owner=uuid4().hex,
        expected_version=arguments.get("expected_version"),
    )


async def read_discord_research_job(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    repository = context.research_jobs
    root_id = context.conversation_root_id
    if repository is None or root_id is None:
        return {"error": "Durable research jobs are not available."}
    scope = await asyncio.to_thread(
        repository.scope, arguments["job_id"], guild_id=context.guild.id,
        conversation_root_id=root_id,
    )
    if scope is None:
        return {"error": "That research job is not available in this conversation."}
    if await accessible_message_channel(context, scope.source_channel_id) is None:
        return {"error": "The research source is no longer accessible."}
    context.state.source_channels.add(scope.source_channel_id)
    await require_evidence_access(context)
    job = await asyncio.to_thread(
        repository.peek, scope.job_id, guild_id=context.guild.id,
        conversation_root_id=root_id, touch=True,
    )
    if job is None:
        return {"error": "That research job has expired."}
    offset = min(len(job.messages), max(0, int(arguments.get("offset", 0))))
    limit = min(25, max(1, int(arguments.get("limit", 25))))
    result = job.manifest()
    result.update({
        "matched_count": len(job.messages),
        "messages": list(job.messages[offset:offset + limit]),
        "next_offset": offset + limit if offset + limit < len(job.messages) else None,
    })
    await require_evidence_access(context)
    return result


async def retain_discord_research_report(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    repository = context.research_jobs
    root_id = context.conversation_root_id
    if repository is None or root_id is None:
        return {"error": "Durable research jobs are not available."}
    scope = await asyncio.to_thread(
        repository.scope, arguments["job_id"], guild_id=context.guild.id,
        conversation_root_id=root_id,
    )
    if scope is None:
        return {"error": "That research job is not available in this conversation."}
    if await accessible_message_channel(context, scope.source_channel_id) is None:
        return {"error": "The research source is no longer accessible."}
    context.state.source_channels.add(scope.source_channel_id)
    await require_evidence_access(context)
    job = await asyncio.to_thread(
        repository.peek, scope.job_id, guild_id=context.guild.id,
        conversation_root_id=root_id,
    )
    if job is None:
        return {"error": "That research job has expired."}
    if job.status not in {"completed", "partial"} or job.continuable:
        return {
            "error": (
                "Finish the research job before retaining it as evidence. "
                "A bounded terminal partial result is allowed."
            )
        }
    try:
        report = DiscordResearchReport.from_job(uuid4().hex, job)
        retain_report(context.state.reports, report)
    except ArtifactCapacityError:
        return {
            "error": (
                "The complete research report is too large to retain in this "
                "conversation."
            )
        }
    await require_evidence_access(context)
    return report.page()


async def read_discord_research_report(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    report = context.state.reports.get(arguments["report_id"])
    if not isinstance(report, DiscordResearchReport) or report.guild_id != context.guild.id:
        return {
            "error": (
                "That Discord research report is not available in this conversation."
            )
        }
    result = report.page(
        offset=arguments.get("offset", 0), limit=arguments.get("limit", 25),
    )
    await require_evidence_access(context)
    return result


async def cancel_discord_research_job(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    repository = context.research_jobs
    root_id = context.conversation_root_id
    if repository is None or root_id is None:
        return {"error": "Durable research jobs are not available."}
    try:
        job = await asyncio.to_thread(
            repository.cancel, arguments["job_id"], guild_id=context.guild.id,
            conversation_root_id=root_id, requester_id=context.member.id,
        )
    except PermissionError:
        return {"error": "Only the member who started that research job can cancel it."}
    if job is None:
        return {"error": "That research job is not available in this conversation."}
    return job.manifest()


def _search_date(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Invalid search date")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _message_time(context: AgentRequestContext) -> datetime:
    value = context.source_message.created_at
    if not isinstance(value, datetime):
        raise ValueError("Invalid request time")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = ["research_tools"]
