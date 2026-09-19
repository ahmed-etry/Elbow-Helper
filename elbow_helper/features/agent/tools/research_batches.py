"""Thin tool adapters for bounded multi-channel Discord research."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Mapping

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import AgentAccessLost, accessible_message_channel, require_evidence_access
from ..models import AgentRequestContext, RegisteredAgentTool
from ..research.contracts import (
    DiscordResearchJob, MAX_RESEARCH_BATCH_JOBS, ResearchJobDefinition,
)
from .discord import HISTORY_PAGE_LIMIT, SEARCH_RESULT_LIMIT


def research_batch_tools() -> tuple[RegisteredAgentTool, ...]:
    definitions = (
        (
            "start_discord_research_batch",
            "Queue the same bounded Discord search or explicit-period history research across two or three currently accessible channels. Jobs remain independent, restart-persistent and permission-rechecked; creation is all-or-none and performs no Discord write.",
            {
                "channel_ids": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                    "minItems": 2,
                    "maxItems": MAX_RESEARCH_BATCH_JOBS,
                    "uniqueItems": True,
                },
                "kind": {"type": "string", "enum": ["search", "history"]},
                "query": {"type": "string", "maxLength": 1024},
                "author_id": {"type": "integer", "minimum": 1},
                "after": {"type": "string", "maxLength": 40},
                "before": {"type": "string", "maxLength": 40},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 25},
            },
            ("channel_ids", "kind"),
            start_discord_research_batch,
        ),
        (
            "read_discord_research_jobs",
            "Read grouped status manifests for up to three retained Discord research jobs after rechecking every current source permission. This returns no message content; use the individual reader for evidence pages.",
            {
                "job_ids": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 32},
                    "minItems": 1,
                    "maxItems": MAX_RESEARCH_BATCH_JOBS,
                    "uniqueItems": True,
                },
            },
            ("job_ids",),
            read_discord_research_jobs,
        ),
        (
            "list_discord_research_jobs",
            "List bounded unexpired Discord research job identities and statuses in this conversation after rechecking each source. Inaccessible jobs are omitted, no message content is loaded, and listing does not extend six-hour idle expiry.",
            {
                "limit": {
                    "type": "integer", "minimum": 1, "maximum": 25,
                },
            },
            (),
            list_discord_research_jobs,
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
    )


async def start_discord_research_batch(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    repository = context.research_jobs
    root_id = context.conversation_root_id
    if repository is None or root_id is None:
        return {"error": "Durable research jobs are not available."}
    channel_ids = arguments.get("channel_ids")
    if (
        not isinstance(channel_ids, list)
        or not 2 <= len(channel_ids) <= MAX_RESEARCH_BATCH_JOBS
        or any(type(value) is not int or value <= 0 for value in channel_ids)
        or len(set(channel_ids)) != len(channel_ids)
    ):
        return {"error": "Choose two or three distinct channel IDs."}
    kind = arguments.get("kind")
    if kind not in {"search", "history"}:
        return {"error": "Choose search or history research."}
    author_id = arguments.get("author_id")
    if author_id is not None and (type(author_id) is not int or author_id <= 0):
        return {"error": "A valid author ID is required."}
    query = str(arguments.get("query") or "").strip()
    try:
        after = _search_date(arguments.get("after"))
        before = _search_date(arguments.get("before")) or _message_time(context)
        if after is not None and after >= before:
            raise ValueError("Invalid research period")
    except ValueError:
        return {"error": "Use a valid research period whose start is before its end."}
    if kind == "history":
        if after is None:
            return {"error": "History research requires a start date."}
        if query:
            return {"error": "History research does not use search words."}
        maximum_page_size, default_page_size = HISTORY_PAGE_LIMIT, 25
    else:
        if not query and not any(
            arguments.get(key) for key in ("author_id", "after", "before")
        ):
            return {"error": "Supply search words, an author, or a date range."}
        maximum_page_size, default_page_size = SEARCH_RESULT_LIMIT, 10
    page_size = arguments.get("page_size", default_page_size)
    if type(page_size) is not int or not 1 <= page_size <= maximum_page_size:
        return {"error": f"Use a page size from 1 to {maximum_page_size}."}
    ordered_channel_ids = tuple(sorted(channel_ids))
    for channel_id in ordered_channel_ids:
        if await accessible_message_channel(context, channel_id) is None:
            return {"error": "The asker cannot access every research source."}
    definitions = tuple(ResearchJobDefinition(
        source_channel_id=channel_id,
        query=query,
        author_id=author_id,
        after=after.isoformat() if after is not None else None,
        before=before.isoformat(),
        page_size=page_size,
        kind=kind,
    ) for channel_id in ordered_channel_ids)
    jobs = await asyncio.to_thread(
        repository.create_many,
        guild_id=context.guild.id,
        conversation_root_id=root_id,
        requester_id=context.member.id,
        definitions=definitions,
    )
    try:
        for channel_id in ordered_channel_ids:
            if await accessible_message_channel(context, channel_id) is None:
                raise AgentAccessLost("Research source access changed")
            context.state.source_channels.add(channel_id)
        await require_evidence_access(context)
    except AgentAccessLost:
        await asyncio.to_thread(
            repository.cancel_many,
            tuple(job.job_id for job in jobs),
            guild_id=context.guild.id,
            conversation_root_id=root_id,
            requester_id=context.member.id,
        )
        raise
    return {
        "job_count": len(jobs),
        "job_ids": [job.job_id for job in jobs],
        "jobs": [job.manifest() for job in jobs],
    }


async def read_discord_research_jobs(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    jobs, error = await _load_scoped_jobs(
        context, arguments.get("job_ids"), minimum=1,
    )
    if error is not None:
        return error
    status_counts: dict[str, int] = {}
    for job in jobs:
        status_counts[job.status] = status_counts.get(job.status, 0) + 1
    return {
        "job_count": len(jobs),
        "status_counts": dict(sorted(status_counts.items())),
        "all_terminal": all(not job.continuable for job in jobs),
        "jobs": [job.manifest() for job in jobs],
    }


async def list_discord_research_jobs(
    context: AgentRequestContext,
    arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    repository = context.research_jobs
    root_id = context.conversation_root_id
    if repository is None or root_id is None:
        return {"error": "Durable research jobs are not available."}
    limit = arguments.get("limit", 20)
    if type(limit) is not int or not 1 <= limit <= 25:
        return {"error": "Choose an inventory limit from one to 25."}
    scopes = await asyncio.to_thread(
        repository.conversation_scopes,
        guild_id=context.guild.id,
        conversation_root_id=root_id,
        limit=limit + 1,
    )
    candidate_limit_reached = len(scopes) > limit
    visible = []
    omitted = 0
    for scope in scopes[:limit]:
        if await accessible_message_channel(context, scope.source_channel_id) is None:
            omitted += 1
            continue
        context.state.source_channels.add(scope.source_channel_id)
        visible.append({
            "job_id": scope.job_id,
            "status": scope.status,
            "source_channel_id": scope.source_channel_id,
            "requester_id": scope.requester_id,
        })
    await require_evidence_access(context)
    return {
        "jobs": visible,
        "returned_jobs": len(visible),
        "inaccessible_jobs_omitted": omitted > 0,
        "candidate_limit_reached": candidate_limit_reached,
        "idle_expiry_extended": False,
    }


async def _load_scoped_jobs(
    context: AgentRequestContext,
    job_ids: Any,
    *,
    minimum: int,
) -> tuple[
    tuple[DiscordResearchJob, ...] | None,
    Mapping[str, Any] | None,
]:
    await require_evidence_access(context)
    repository = context.research_jobs
    root_id = context.conversation_root_id
    if repository is None or root_id is None:
        return None, {"error": "Durable research jobs are not available."}
    if (
        not isinstance(job_ids, list)
        or not minimum <= len(job_ids) <= MAX_RESEARCH_BATCH_JOBS
        or any(not isinstance(value, str) or len(value) != 32 for value in job_ids)
        or len(set(job_ids)) != len(job_ids)
    ):
        minimum_label = "one" if minimum == 1 else "two"
        return None, {
            "error": (
                f"Choose {minimum_label} to three distinct research job IDs."
            )
        }
    scopes = []
    for job_id in job_ids:
        scope = await asyncio.to_thread(
            repository.scope,
            job_id,
            guild_id=context.guild.id,
            conversation_root_id=root_id,
        )
        if scope is None:
            return None, {
                "error": "A research job is not available in this conversation."
            }
        scopes.append(scope)
    for scope in scopes:
        if await accessible_message_channel(context, scope.source_channel_id) is None:
            return None, {"error": "A research source is no longer accessible."}
        context.state.source_channels.add(scope.source_channel_id)
    await require_evidence_access(context)
    jobs = []
    for scope in scopes:
        job = await asyncio.to_thread(
            repository.peek,
            scope.job_id,
            guild_id=context.guild.id,
            conversation_root_id=root_id,
            touch=True,
        )
        if job is None:
            return None, {"error": "A research job has expired."}
        jobs.append(job)
    await require_evidence_access(context)
    return tuple(jobs), None


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


__all__ = [
    "list_discord_research_jobs", "read_discord_research_jobs",
    "research_batch_tools",
    "start_discord_research_batch",
]
