"""ID-verified join and leave evidence from all lifecycle log authors."""

import asyncio
import json
import re
import time

import discord

from elbow_helper.configuration.channels import OVERSEEING_TERRACE
from elbow_helper.discord.message_search import DiscordMessageSearchError
from ...access import accessible_message_channel, require_evidence_access
from ...engine.budgets import TOOL_TIMEOUT_SECONDS
from ...research.history import (
    _history_cursor, _historycursor_state, jump_url, request_message_boundary,
    search_message_text,
)


def lifecycle_entry(message, member_id, guild):
    # Verify raw evidence before mention rendering or truncating the excerpt.
    raw = message.content + "\n" + json.dumps(message.embeds, ensure_ascii=False)
    if not re.search(rf"(?<!\d){member_id}(?!\d)", raw):
        return None
    text = search_message_text(message, guild)
    # Logs from older bots use both titles and plain sentences.
    event = re.search(r"\b(join(?:ed|s)?|leav(?:e|es|ing)|left|departed|kicked|banned)\b",
                      text, re.IGNORECASE)
    if event is None or not message.author_is_bot:
        return None
    return {
        "message_id": message.message_id, "channel_id": message.channel_id,
        "event": "join" if event.group().lower().startswith("join") else "leave",
        "timestamp": message.timestamp, "author_id": message.author_id,
        "author": message.author_name, "content": text[:1600],
        "source": jump_url(guild.id, message.channel_id, message.message_id),
    }


async def read_member_history(context, arguments):
    await require_evidence_access(context)
    if await accessible_message_channel(context, OVERSEEING_TERRACE) is None:
        return {"error": "The member lifecycle source is not accessible."}
    member_id = arguments.get("member_id")
    if type(member_id) is not int or member_id <= 0:
        return {"error": "Supply a valid member ID."}
    scope = {"guild_id": context.guild.id, "channel_id": OVERSEEING_TERRACE,
             "member_id": member_id, "after_id": 1}
    try:
        before, cursor_scope = _historycursor_state(
            arguments.get("cursor"), scope, request_message_boundary(context),
        )
    except ValueError:
        return {"error": "That continuation cursor does not match this member history."}
    deadline = time.monotonic() + TOOL_TIMEOUT_SECONDS - 3
    if context.deadline_monotonic is not None:
        deadline = min(deadline, context.deadline_monotonic - 1)
    entries, limits = {}, []
    exhaustive = False

    def collect(messages):
        for message in messages:
            if message.channel_id != OVERSEEING_TERRACE or message.message_id >= before:
                continue
            entry = lifecycle_entry(message, member_id, context.guild)
            if entry:
                entries[message.message_id] = entry

    try:
        async with asyncio.timeout(max(0.1, deadline - time.monotonic())):
            if not arguments.get("cursor"):
                member = context.guild.get_member(member_id)
                if member is None:
                    try:
                        member = await context.bot.fetch_user(member_id)
                    except (discord.HTTPException, AttributeError):
                        member = None
                names = tuple(dict.fromkeys(
                    str(value) for value in (
                        getattr(member, "name", None), getattr(member, "display_name", None),
                    ) if value
                ))
                # Separate queries form a union, rather than an AND that drops logs.
                for query, mentions in (("", (member_id,)), *((name, ()) for name in names)):
                    offset = 0
                    try:
                        async with asyncio.timeout(2):
                            for _ in range(4):
                                page = await context.message_search.search_page(
                                    guild_id=context.guild.id, content=query, mentions=mentions,
                                    channel_ids=(OVERSEEING_TERRACE,), limit=25,
                                    offset=offset, max_id=before,
                                )
                                collect(page.messages)
                                if page.next_offset is None:
                                    break
                                if page.next_offset <= offset:
                                    raise DiscordMessageSearchError("Search cursor did not advance")
                                offset = page.next_offset
                    except (TimeoutError, DiscordMessageSearchError, discord.HTTPException):
                        # History still works when the index is incomplete or unavailable.
                        limits.append("indexed_search_unavailable")
            # Discord search does not reliably index embeds or old names. Scan the
            # channel before declaring the history exhaustive, including departed users.
            for _ in range(100):
                page = await context.message_search.history_page(
                    channel_id=OVERSEEING_TERRACE, before_id=before, after_id=1, limit=100,
                )
                collect(page.messages)
                if page.reached_window_start:
                    exhaustive = True
                    break
                if page.next_before_id is None or page.next_before_id >= before:
                    raise ValueError("History cursor did not advance")
                before = page.next_before_id
    except (TimeoutError, discord.HTTPException, RuntimeError, ValueError):
        limits.append("history_incomplete")
    if await accessible_message_channel(context, OVERSEEING_TERRACE) is None:
        return {"error": "The member lifecycle source is not accessible."}
    context.state.source_channels.add(OVERSEEING_TERRACE)
    await require_evidence_access(context)
    if not exhaustive and before < cursor_scope["effective_before_id"]:
        # Leave older indexed hits to the next scan page, so continuations do
        # not repeat evidence already returned by a name or mention query.
        entries = {key: value for key, value in entries.items() if key >= before}
    return {
        "member_id": member_id, "channel_id": OVERSEEING_TERRACE,
        "entries": sorted(entries.values(), key=lambda row: row["message_id"]),
        "search_is_exhaustive": exhaustive, "complete": exhaustive,
        "next_cursor": None if exhaustive or before == cursor_scope["effective_before_id"]
                       else _history_cursor(before, cursor_scope),
        "coverage": {"currently_available_messages_only": True,
                     "continued": bool(arguments.get("cursor"))},
        "limitations": limits,
    }
