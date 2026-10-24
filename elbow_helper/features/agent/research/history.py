"""Discord history tools with asker-specific permission enforcement."""

from __future__ import annotations

from typing import Any
from typing import Mapping
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import discord


from ..access import accessible_message_channel, can_access_message_channel
from ..models import AgentRequestContext
from ..text import message_text
from ..capabilities.validation import bounded_int
from ..capabilities.validation import positive_int


SEARCH_RESULT_LIMIT = 25
CONTEXT_MESSAGE_LIMIT = 50
HISTORY_PAGE_LIMIT = 25
INTERACTIVE_HISTORY_PAGE_LIMIT = 100
HISTORY_MESSAGE_CHARACTER_BUDGET = 12_000
COMPLETE_HISTORY_CHARACTER_BUDGET = 48_000
def search_message_text(message, guild) -> str:
    return message_text(SimpleNamespace(
        content=message.content,
        embeds=[discord.Embed.from_dict(item) for item in message.embeds],
        attachments=[SimpleNamespace(filename=name) for name in message.attachment_names],
        guild=guild,
    ))


async def read_discord_channel_history(
    context: AgentRequestContext,
    arguments: Mapping[str, Any], *, complete_content: bool = False,
) -> Mapping[str, Any]:
    # A cursor already issued in this request carries its original window and
    # author selection. Omitting them avoids copying or changing the window.
    if "text_filter" in arguments:
        return {"error": "Read conversation chunks without a text filter; select relevant messages by meaning."}
    token = arguments.get("cursor")
    saved = context.state.history_cursors.get(token) if isinstance(token, str) else None
    if saved is not None:
        arguments = {**saved, **arguments}
    pages = arguments.get("pages", 1)
    if type(pages) is not int or not 1 <= pages <= 8:
        return {"error": "Choose between one and eight history pages."}
    result = None
    remaining = COMPLETE_HISTORY_CHARACTER_BUDGET if complete_content else HISTORY_MESSAGE_CHARACTER_BUDGET
    page_arguments = dict(arguments)
    for _ in range(pages):
        page = await _read_history_page(context, page_arguments, character_budget=remaining,
                                        **({"complete_content": True} if complete_content else {}))
        if "error" in page:
            # Access failures must discard earlier pages, too.
            return page
        batch = page["messages"]
        remaining -= sum(len(json.dumps(list(readable_history_row(row).values()) if complete_content else row,
            ensure_ascii=False, separators=(",", ":"))) + 1 for row in batch)
        if result is None:
            result = page
        else:
            result["messages"].extend(batch)
            previous = result["coverage"]
            coverage = page["coverage"]
            for field in ("scanned_messages_in_window", "returned_messages", "scanned_count", "matched_count"):
                previous[field] += coverage[field]
            for field in ("next_cursor", "reached_requested_start", "stopped_at_message_budget"):
                previous[field] = coverage[field]
        cursor = page["coverage"]["next_cursor"]
        if (cursor is None or remaining <= 0 or page["coverage"]["scanned_count"] == 0
                or page["coverage"]["stopped_at_message_budget"]):
            break
        page_arguments["cursor"] = cursor
    cursor = result["coverage"]["next_cursor"]
    if cursor is not None:
        context.state.history_cursors[cursor] = {
            key: value for key, value in arguments.items()
            if key in ("channel_id", "after", "before", "author_id")
        }
    result["coverage"]["pages_scanned"] = _ + 1
    return result


async def _read_history_page(context, arguments, *, character_budget, complete_content=False):
    channel_id = positive_int(arguments.get("channel_id"))
    author_id = arguments.get("author_id")
    if channel_id is None or (
        author_id is not None and positive_int(author_id) is None
    ):
        return {"error": "Valid channel and author IDs are required."}
    channel = await accessible_message_channel(context, channel_id)
    if channel is None:
        return {"error": "That conversation is not available here."}
    try:
        after = search_date(arguments.get("after"))
        before = search_date(arguments.get("before"))
        effective_before = before or _request_time(context)
        if after is not None and after >= effective_before:
            raise ValueError("Invalid history period")
    except ValueError:
        return {
            "error": (
                "Use a valid ISO 8601 start before the requested history end."
            )
        }
    after_id = discord.utils.time_snowflake(after) - 1 if after is not None else 0
    requested_before_id = (
        discord.utils.time_snowflake(before) if before is not None else None
    )
    if after_id < 0:
        return {"error": "The requested history start is outside Discord history."}
    base_scope = {
        "guild_id": context.guild.id,
        "channel_id": channel_id,
        "author_id": author_id,
        "after_id": after_id,
        "requested_before_id": requested_before_id,
    }
    try:
        page_before_id, cursor_scope = _historycursor_state(
            arguments.get("cursor"), base_scope,
            requested_before_id or request_message_boundary(context),
        )
    except ValueError:
        return {
            "error": (
                "That continuation cursor does not match this channel history "
                "period and its filters."
            )
        }
    limit = bounded_int(
        arguments.get("limit"), default=HISTORY_PAGE_LIMIT,
        minimum=1, maximum=INTERACTIVE_HISTORY_PAGE_LIMIT,
    )
    page = await context.message_search.history_page(
        channel_id=channel_id, before_id=page_before_id,
        after_id=after_id, limit=limit,
    )
    if await accessible_message_channel(context, channel_id) is None:
        return {"error": "That conversation is not available here."}
    if (
        page.before_id != page_before_id
        or page.after_id != after_id
        or page.limit != limit
        or page.reached_window_start != (page.next_before_id is None)
        or (
            page.next_before_id is not None
            and not after_id < page.next_before_id < page_before_id
        )
        or any(
            message.channel_id != channel_id
            or not after_id < message.message_id < page_before_id
            for message in page.messages
        )
    ):
        return {
            "error": "Discord returned messages outside the requested history scope."
        }
    messages = []
    message_characters = 0
    scanned_messages = 0
    stopped_at_message_budget = False
    next_before_id = page.next_before_id
    for message in page.messages:
        if author_id is not None and message.author_id != author_id:
            scanned_messages += 1
            continue
        content = message.content if complete_content else search_message_text(message, context.guild)
        entry = {
            "message_id": message.message_id,
            "channel_id": channel_id,
            "channel": getattr(channel, "name", str(channel_id)),
            "author_id": message.author_id,
            "author": message.author_name,
            "timestamp": message.timestamp,
            "content": content,
            "content_complete": True,
            "source": jump_url(context.guild.id, channel_id, message.message_id),
        }
        if complete_content:
            entry.update(embeds=readable_embeds(message.embeds),
                         attachment_names=list(message.attachment_names),
                         attachment_contents_read=False, content_complete=True,
                         reply_to_message_id=message.reply_to_message_id)
        if message.reply_to_message_id is not None:
            entry["reply_to_message_id"] = message.reply_to_message_id
        # Complete family chunks use a uniform record shape, encoded as columns
        # and rows for the model. Budget that readable representation rather
        # than repeating every JSON field name for every short chat message.
        entry_characters = len(json.dumps(list(readable_history_row(entry).values()) if complete_content else entry,
                                          ensure_ascii=False, separators=(",", ":"))) + 1
        if (message_characters + entry_characters > character_budget
                and (messages or character_budget < HISTORY_MESSAGE_CHARACTER_BUDGET)):
            # Discord pages are newest first. Resume immediately before the
            # last examined message, including filtered-out authors, so no
            # omitted matching message is skipped when a large page is cut.
            next_before_id = (page.messages[scanned_messages - 1].message_id
                              if scanned_messages else page_before_id)
            stopped_at_message_budget = True
            break
        messages.append(entry)
        message_characters += entry_characters
        scanned_messages += 1
    context.state.source_channels.add(channel_id)
    return {
        "channel_id": channel_id,
        "channel": getattr(channel, "name", str(channel_id)),
        "messages": messages,
        "coverage": {
            "order": "newest_first",
            "requested_after": after.isoformat() if after is not None else None,
            "requested_before": before.isoformat() if before is not None else None,
            "window_after_message_id": after_id,
            "snapshot_before_message_id": cursor_scope["effective_before_id"],
            "page_before_message_id": page.before_id,
            "scanned_messages_in_window": scanned_messages,
            "returned_messages": len(messages),
            "scanned_count": scanned_messages,
            "matched_count": len(messages),
            "stopped_at_message_budget": stopped_at_message_budget,
            "next_cursor": (
                _history_cursor(next_before_id, cursor_scope)
                if next_before_id is not None else None
            ),
            "reached_requested_start": next_before_id is None,
            "covers_currently_available_messages_only": True,
        },
    }


def readable_embeds(embeds):
    """Keep all embed text, excluding binary placeholders and media transport metadata."""
    rows = []
    for embed in embeds:
        row = {key: embed[key] for key in ("title", "description") if key in embed}
        for key, field in (("author", "name"), ("footer", "text")):
            if field in embed.get(key, {}):
                row[key] = {field: embed[key][field]}
        if embed.get("fields"):
            row["fields"] = [{key: field[key] for key in ("name", "value") if key in field}
                             for field in embed["fields"]]
        if row:
            rows.append(row)
    return rows


def readable_history_row(row):
    """Channel and content-status metadata are shared by the whole chunk."""
    return {key: value for key, value in row.items()
            if key not in ("channel_id", "channel", "content_complete", "attachment_contents_read")}


def cursor_state(
    value: Any,
    base_scope: Mapping[str, Any],
    default_max_id: int,
) -> tuple[int, int, dict[str, Any]]:
    if value is None:
        scope = {**base_scope, "effective_max_id": default_max_id}
        return 0, default_max_id, scope
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Invalid Discord search cursor")
    parts = value.split(".")
    if (
        len(parts) != 4
        or parts[0] != "v1"
        or not parts[1].isdigit()
        or not parts[2].isdigit()
    ):
        raise ValueError("Invalid Discord search cursor")
    offset = int(parts[1])
    max_id = int(parts[2])
    scope = {**base_scope, "effective_max_id": max_id}
    if (
        not 0 <= offset <= 9_975
        or max_id <= 0
        or parts[3] != _scope_digest(scope)
    ):
        raise ValueError("Invalid Discord search cursor")
    return offset, max_id, scope


def search_cursor(offset: int, scope: Mapping[str, Any]) -> str:
    return (
        f"v1.{offset}.{scope['effective_max_id']}.{_scope_digest(scope)}"
    )


def _historycursor_state(
    value: Any,
    base_scope: Mapping[str, Any],
    default_before_id: int,
) -> tuple[int, dict[str, Any]]:
    if value is None:
        if default_before_id <= base_scope["after_id"]:
            raise ValueError("Invalid Discord history window")
        scope = {**base_scope, "effective_before_id": default_before_id}
        return default_before_id, scope
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Invalid Discord history cursor")
    parts = value.split(".")
    if (
        len(parts) != 4
        or parts[0] != "h1"
        or not parts[1].isdigit()
        or not parts[2].isdigit()
    ):
        raise ValueError("Invalid Discord history cursor")
    page_before_id = int(parts[1])
    effective_before_id = int(parts[2])
    scope = {**base_scope, "effective_before_id": effective_before_id}
    if (
        not base_scope["after_id"] < page_before_id < effective_before_id
        or parts[3] != _scope_digest(scope)
    ):
        raise ValueError("Invalid Discord history cursor")
    return page_before_id, scope


def _history_cursor(before_id: int, scope: Mapping[str, Any]) -> str:
    return (
        f"h1.{before_id}.{scope['effective_before_id']}."
        f"{_scope_digest(scope)}"
    )


def _scope_digest(scope: Mapping[str, Any]) -> str:
    payload = json.dumps(scope, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def request_message_boundary(context: AgentRequestContext) -> int:
    message_id = getattr(context.source_message, "id", None)
    if type(message_id) is int and message_id > 0:
        return message_id
    created_at = getattr(context.source_message, "created_at", None)
    if not isinstance(created_at, datetime):
        raise ValueError("Missing Discord search snapshot boundary")
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return discord.utils.time_snowflake(created_at.astimezone(timezone.utc))


def _request_time(context: AgentRequestContext) -> datetime:
    created_at = getattr(context.source_message, "created_at", None)
    if not isinstance(created_at, datetime):
        raise ValueError("Missing Discord request time")
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at.astimezone(timezone.utc)


def search_date(value: Any) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


async def find_discord_channels(context: AgentRequestContext, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    query = str(arguments["query"]).strip().casefold()
    numeric = query.removeprefix("<#").removesuffix(">")
    if numeric.isdecimal():
        channel = await accessible_message_channel(context, int(numeric))
        if channel is None:
            return {"channels": [], "matched_count": 0, "truncated": False}
        context.state.source_channels.add(channel.id)
        return {"channels": [{"channel_id": channel.id, "name": channel.name}], "matched_count": 1, "truncated": False}
    channels = []
    for channel in (*context.guild.channels, *context.guild.threads):
        name = str(getattr(channel, "name", ""))
        if query.lstrip("#") in name.casefold() and await can_access_message_channel(
            context, channel, relevant=bool(query),
        ):
            channels.append({"channel_id": channel.id, "name": name})
    context.state.source_channels.update(item["channel_id"] for item in channels[:20])
    return {"channels": channels[:20], "matched_count": len(channels), "truncated": len(channels) > 20}


async def read_message_context(
    context: AgentRequestContext,
    arguments: Mapping[str, Any], *, complete_content: bool = False,
) -> Mapping[str, Any]:
    channel_id = positive_int(arguments.get("channel_id"))
    message_id = positive_int(arguments.get("message_id"))
    if channel_id is None or message_id is None:
        return {"error": "Valid channel_id and message_id values are required."}
    channel = await accessible_message_channel(context, channel_id)
    if channel is None:
        return {"error": "That conversation is not available here."}
    history = getattr(channel, "history", None)
    if not callable(history):
        return {"error": "That Discord location does not expose message history."}
    limit = bounded_int(
        arguments.get("limit"),
        default=15,
        minimum=3,
        maximum=CONTEXT_MESSAGE_LIMIT,
    )
    try:
        after = search_date(arguments.get("after"))
        before = search_date(arguments.get("before"))
    except ValueError:
        return {"error": "invalid_period"}
    if after is not None or before is not None:
        if after is None or before is None or after >= before:
            return {"error": "invalid_period"}
        lower = discord.utils.time_snowflake(after) - 1
        upper = discord.utils.time_snowflake(before)
        if not lower < message_id < upper:
            return {"error": "message_out_of_period"}
        preceding = [message async for message in history(
            limit=limit // 2 + 1, after=discord.Object(id=lower),
            before=discord.Object(id=message_id + 1), oldest_first=False,
        )]
        following = [message async for message in history(
            limit=limit - len(preceding), after=discord.Object(id=message_id),
            before=discord.Object(id=upper), oldest_first=True,
        )]
        messages = [*reversed(preceding), *following]
    else:
        messages = [message async for message in history(
            limit=limit, around=discord.Object(id=message_id), oldest_first=True,
        )]
    anchor = next((message for message in messages if message.id == message_id), None)
    reply_target = await _read_reply_target(
        context.guild.id, channel, anchor, messages, after=after, before=before, complete_content=complete_content,
    ) if anchor is not None else None
    if await accessible_message_channel(context, channel_id) is None:
        return {"error": "That conversation is not available here."}
    context.state.source_channels.add(channel_id)
    return {
        "channel_id": channel_id,
        "channel": getattr(channel, "name", str(channel_id)),
        "requested_message_id": message_id,
        "requested_message_found": anchor is not None,
        "reply_target": reply_target,
        "messages": [
            {**discord_message_evidence(context.guild.id, message, complete_content=complete_content),
             "is_requested_message": message.id == message_id}
            for message in messages
        ],
    }


async def _read_reply_target(guild_id, channel, anchor, messages, *, after, before, complete_content=False):
    """Resolve one explicit same-channel reply, independently of nearby topics."""
    target_id = discord_message_evidence(guild_id, anchor).get("reply_to_message_id")
    if target_id is None:
        return None
    if after is not None and not (
        discord.utils.time_snowflake(after) - 1 < target_id < discord.utils.time_snowflake(before)
    ):
        return {"message_id": target_id, "status": "outside_requested_period"}
    target = next((message for message in messages if message.id == target_id), None)
    if target is None:
        fetch = getattr(channel, "fetch_message", None)
        if callable(fetch):
            try:
                target = await fetch(target_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                target = None
    if (target is None or target.id != target_id
            or target.channel.id != channel.id):
        return {"message_id": target_id, "status": "unavailable"}
    return {**discord_message_evidence(guild_id, target, complete_content=complete_content), "status": "read"}


async def searchable_channel_ids(context: AgentRequestContext, *, limit: int | None = 500) -> tuple[int, ...]:
    allowed = []
    for channel in (*context.guild.channels, *context.guild.threads):
        if isinstance(channel, (discord.CategoryChannel, discord.StageChannel, discord.VoiceChannel)):
            continue
        if await can_access_message_channel(context, channel, relevant=False):
            allowed.append(channel.id)
            if limit is not None and len(allowed) >= limit:
                break
    return tuple(allowed)


def discord_message_evidence(guild_id: int, message: discord.Message, *, complete_content=False) -> dict[str, Any]:
    content = getattr(message, "content", "") if complete_content else message_text(message)
    entry = {
        "message_id": message.id,
        "author_id": message.author.id,
        "author": message.author.display_name,
        "timestamp": message.created_at.isoformat(),
        "content": content,
        "content_complete": True,
        "source": jump_url(guild_id, message.channel.id, message.id),
    }
    if complete_content:
        entry.update(content_complete=True, mentioned_member_ids=list(getattr(message, "raw_mentions", ())),
                     embeds=readable_embeds(embed.to_dict() for embed in getattr(message, "embeds", ())),
                     attachment_contents_read=False,
                     attachments=[{"filename": item.filename, "url": getattr(item, "url", None),
                                   "size": getattr(item, "size", None)}
                                  for item in getattr(message, "attachments", ())])
    reference = getattr(message, "reference", None)
    target = getattr(reference, "message_id", None)
    reference_channel = getattr(reference, "channel_id", None) or message.channel.id
    reference_type = getattr(reference, "type", discord.MessageReferenceType.default)
    if (type(target) is int and target > 0 and reference_channel == message.channel.id
            and reference_type == discord.MessageReferenceType.default):
        entry["reply_to_message_id"] = target
    return entry


def jump_url(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
