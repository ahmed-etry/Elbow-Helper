"""One-page execution service shared by foreground and background research."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any, Mapping

import discord

from elbow_helper.discord.message_search import DiscordMessageSearchError

from ..access import AgentAccessLost, accessible_message_channel, require_evidence_access
from .repository import ResearchJobBusy, ResearchJobConflict
from ..models import AgentRequestContext
from ..tools.discord import (
    read_discord_channel_history, search_discord_messages,
)


async def advance_discord_research_job(
    context: AgentRequestContext, job_id: str, *, lease_owner: str,
    expected_version: int | None = None,
) -> Mapping[str, Any]:
    """Claim, read and checkpoint one authorized page without model work."""

    repository = context.research_jobs
    root_id = context.conversation_root_id
    if repository is None or root_id is None:
        return {"error": "Durable research jobs are not available."}
    scope = await asyncio.to_thread(
        repository.scope, job_id, guild_id=context.guild.id,
        conversation_root_id=root_id,
    )
    if scope is None:
        return {"error": "That research job is not available in this conversation."}
    if await accessible_message_channel(context, scope.source_channel_id) is None:
        return {"error": "The research source is no longer accessible."}
    context.state.source_channels.add(scope.source_channel_id)
    await require_evidence_access(context)
    try:
        claimed = await asyncio.to_thread(
            repository.claim, scope.job_id, guild_id=context.guild.id,
            conversation_root_id=root_id, lease_owner=lease_owner,
            expected_version=expected_version,
        )
    except ResearchJobBusy:
        return {"error": "That research job is already being advanced."}
    except ResearchJobConflict:
        return {
            "error": (
                "That research job changed. Read its current status before "
                "continuing it."
            )
        }
    if claimed is None:
        return {"error": "That research job has expired."}
    if claimed.status != "running":
        return claimed.manifest()
    page_arguments = {
        "channel_id": claimed.source_channel_id,
        "limit": claimed.page_size,
    }
    if claimed.author_id is not None:
        page_arguments["author_id"] = claimed.author_id
    if claimed.after is not None:
        page_arguments["after"] = claimed.after
    if claimed.before is not None:
        page_arguments["before"] = claimed.before
    if claimed.cursor is not None:
        page_arguments["cursor"] = claimed.cursor
    try:
        if claimed.kind == "search":
            page_arguments["query"] = claimed.query
            page = await search_discord_messages(context, page_arguments)
            page_messages = page.get("matches")
        else:
            page = await read_discord_channel_history(context, page_arguments)
            page_messages = page.get("messages")
        if "error" in page:
            await asyncio.to_thread(
                repository.release_retryable, claimed, lease_owner=lease_owner,
                error_class="search_unavailable",
            )
            return page
        coverage = page.get("coverage")
        if not isinstance(coverage, Mapping) or not isinstance(page_messages, list):
            raise ValueError("Research page omitted bounded results or coverage")
        job = await asyncio.to_thread(
            repository.checkpoint_page,
            claimed,
            lease_owner=lease_owner,
            page_messages=page_messages,
            coverage=coverage,
        )
    except AgentAccessLost:
        await _release_job(repository, claimed, lease_owner, "access_lost")
        raise
    except (
        DiscordMessageSearchError, ResearchJobConflict, discord.DiscordException,
        OSError, RuntimeError, sqlite3.Error, TypeError, ValueError,
    ):
        await _release_job(repository, claimed, lease_owner, "read_failed")
        raise
    result = job.manifest()
    result["new_messages"] = page_messages if job.status != "cancelled" else []
    return result


async def _release_job(repository, claimed, lease_owner: str, error_class: str) -> None:
    try:
        await asyncio.to_thread(
            repository.release_retryable, claimed,
            lease_owner=lease_owner, error_class=error_class,
        )
    except ResearchJobConflict:
        pass


__all__ = ["advance_discord_research_job"]
