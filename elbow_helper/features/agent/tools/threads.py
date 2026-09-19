"""Permission-aware active and archived Discord thread discovery."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any, Mapping

import discord

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import accessible_message_channel, can_access_message_channel
from ..models import AgentRequestContext, RegisteredAgentTool
from .shared import bounded_int, positive_int


THREAD_RESULT_LIMIT = 10
THREAD_ARCHIVE_SCAN_LIMIT = 25


def thread_tools() -> tuple[RegisteredAgentTool, ...]:
    return (
        RegisteredAgentTool(
            AgentToolDefinition(
                name="find_discord_threads",
                description=(
                    "Discover currently accessible active or archived threads under "
                    "one exact Discord channel. Public and private discovery are "
                    "separate; private results require current membership. This never "
                    "joins or unarchives a thread, and incomplete private scans are "
                    "reported honestly."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "parent_channel_id": {"type": "integer", "minimum": 1},
                        "state": {
                            "type": "string", "enum": ["active", "archived"],
                        },
                        "visibility": {
                            "type": "string", "enum": ["public", "private"],
                        },
                        "query": {"type": "string", "maxLength": 100},
                        "cursor": {"type": "string", "maxLength": 64},
                        "limit": {
                            "type": "integer", "minimum": 1,
                            "maximum": THREAD_RESULT_LIMIT, "default": 10,
                        },
                    },
                    "required": ["parent_channel_id", "state", "visibility"],
                    "additionalProperties": False,
                },
            ),
            find_discord_threads,
        ),
    )


async def find_discord_threads(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    parent_id = positive_int(arguments.get("parent_channel_id"))
    state = arguments.get("state")
    visibility = arguments.get("visibility")
    if (
        parent_id is None or state not in {"active", "archived"}
        or visibility not in {"public", "private"}
    ):
        return {"error": "Valid parent, thread state and visibility are required."}
    if context.thread_discovery is None:
        return {"error": "Discord thread discovery is not available."}
    parent = await accessible_message_channel(context, parent_id)
    if parent is None:
        return {"error": "The asker cannot access that conversation."}
    archived_threads = getattr(parent, "archived_threads", None)
    if not callable(archived_threads):
        return {"error": "That Discord location cannot contain discoverable threads."}
    private = visibility == "private"
    if private and not isinstance(parent, discord.TextChannel):
        return {"error": "That Discord location does not support private threads."}
    mode = _thread_discovery_mode(context, parent, state, private)
    scope = {
        "guild_id": context.guild.id, "parent_channel_id": parent_id,
        "state": state, "visibility": visibility,
        "query": str(arguments.get("query") or "").strip().casefold(),
        "mode": mode,
    }
    try:
        before_id = _thread_cursor_state(arguments.get("cursor"), scope)
    except ValueError:
        return {"error": "That continuation cursor does not match this thread discovery."}
    limit = bounded_int(
        arguments.get("limit"), default=THREAD_RESULT_LIMIT,
        minimum=1, maximum=THREAD_RESULT_LIMIT,
    )
    if state == "active":
        threads = await context.thread_discovery.active_threads(context.guild)
        if not await can_access_message_channel(context, parent):
            return {"error": "The asker cannot access that conversation."}
        candidates = sorted(
            (
                thread for thread in threads
                if getattr(thread, "parent_id", None) == parent_id
                and bool(thread.is_private()) == private
                and not bool(getattr(thread, "archived", False))
                and (before_id is None or thread.id < before_id)
            ),
            key=lambda thread: thread.id, reverse=True,
        )
        accessible = await _matching_threads(context, candidates, scope["query"])
        returned = accessible[:limit]
        complete = not private and len(accessible) <= limit
        next_before_id = (
            returned[-1].id if len(accessible) > limit and returned else None
        )
        private_scope = "bot_visible_and_requester_joined" if private else None
    else:
        joined = mode == "private_joined"
        page = await context.thread_discovery.archived_page(
            parent, private=private, joined=joined, before_id=before_id,
            limit=THREAD_ARCHIVE_SCAN_LIMIT,
        )
        if page.mode != mode or not await can_access_message_channel(context, parent):
            return {"error": "The asker cannot access that conversation."}
        accessible = await _matching_threads(context, page.threads, scope["query"])
        returned = accessible[:limit]
        complete = (
            not private and page.endpoint_exhausted
            and len(accessible) <= limit
        )
        if len(accessible) > limit:
            next_before_id = _thread_before_marker(returned[-1], joined=joined)
        elif not page.endpoint_exhausted and returned:
            next_before_id = _thread_before_marker(returned[-1], joined=joined)
        elif not private and not page.endpoint_exhausted:
            next_before_id = page.next_before_id
        else:
            next_before_id = None
        private_scope = "bot_visible_and_requester_joined" if private else None
    context.state.source_channels.add(parent_id)
    context.state.source_channels.update(thread.id for thread in returned)
    return {
        "parent_channel_id": parent_id,
        "parent_channel": getattr(parent, "name", str(parent_id)),
        "state": state,
        "visibility": visibility,
        "threads": [_thread_result(thread) for thread in returned],
        "coverage": {
            "returned_threads": len(returned),
            "next_cursor": (
                _thread_cursor(next_before_id, scope)
                if next_before_id is not None else None
            ),
            "coverage_complete": complete,
            "continuable": next_before_id is not None,
            "private_listing_scope": private_scope,
            "active_threads_may_change_between_pages": state == "active",
            "incomplete_private_scan_does_not_prove_no_matching_thread": private,
        },
    }


async def _matching_threads(
    context: AgentRequestContext, threads, query: str,
) -> list[Any]:
    matched = []
    for thread in threads:
        if not await can_access_message_channel(context, thread):
            continue
        name = str(getattr(thread, "name", ""))
        if query and query.lstrip("#") not in name.casefold():
            continue
        matched.append(thread)
    return matched


def _thread_discovery_mode(
    context: AgentRequestContext, parent: Any, state: str, private: bool,
) -> str:
    if state == "active":
        return "active_private" if private else "active_public"
    if not private:
        return "public"
    permissions = parent.permissions_for(context.guild.me)
    return "private_all" if permissions.manage_threads else "private_joined"


def _thread_before_marker(thread: Any, *, joined: bool) -> int:
    if joined:
        return thread.id
    archived_at = getattr(thread, "archive_timestamp", None)
    if not isinstance(archived_at, datetime):
        raise ValueError("Archived thread omitted its archive timestamp")
    return discord.utils.time_snowflake(archived_at, high=False)


def _thread_result(thread: Any) -> dict[str, Any]:
    archived_at = getattr(thread, "archive_timestamp", None)
    return {
        "thread_id": thread.id,
        "name": thread.name,
        "parent_channel_id": thread.parent_id,
        "visibility": "private" if thread.is_private() else "public",
        "archived": bool(thread.archived),
        "archive_timestamp": (
            archived_at.isoformat() if isinstance(archived_at, datetime) else None
        ),
    }


def _thread_cursor_state(value: Any, scope: Mapping[str, Any]) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Invalid thread cursor")
    parts = value.split(".")
    if (
        len(parts) != 3 or parts[0] != "t1" or not parts[1].isdigit()
        or int(parts[1]) <= 0 or parts[2] != _scope_digest(scope)
    ):
        raise ValueError("Invalid thread cursor")
    return int(parts[1])


def _thread_cursor(before_id: int, scope: Mapping[str, Any]) -> str:
    return f"t1.{before_id}.{_scope_digest(scope)}"


def _scope_digest(scope: Mapping[str, Any]) -> str:
    payload = json.dumps(scope, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


__all__ = ["thread_tools"]
