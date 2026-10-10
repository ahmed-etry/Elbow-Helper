"""Delete an explicitly reviewed message selection without broad history purges."""

from datetime import datetime, timedelta, timezone
import logging

import discord

from ..actions.values import display_value
from .management import ID, complete, guard, queue, state_action, tool
from .safety import DiscordActionRefused, resolve_channel


LOGGER = logging.getLogger(__name__)
MESSAGE_IDS = {"type": "array", "items": ID, "minItems": 1, "maxItems": 100,
               "uniqueItems": True}


def cleanup_tools():
    return (tool(
        "delete_discord_messages", "Delete exactly the selected messages after confirmation.",
        {"channel_id": ID, "message_ids": MESSAGE_IDS}, ("channel_id", "message_ids"),
        prepare_cleanup, irreversible=True,
        entities=(("channel_id", "discord_channel"), ("message_ids", "discord_message_set")),
    ),)


def message_state(message):
    return {"id": message.id, "author_id": message.author.id, "content": message.content,
            "created_at": message.created_at.isoformat(), "pinned": message.pinned,
            "attachments": [(item.id, item.filename, item.size) for item in message.attachments]}


def message_lines(message):
    excerpt = " ".join(message.content.split())[:120] or "No text"
    pinned = " (pinned)" if message.pinned else ""
    author = discord.utils.escape_markdown(message.author.display_name)
    lines = [f"{message.jump_url}: {author}, {display_value(message.created_at)}: {excerpt}{pinned}"]
    if message.attachments:
        lines.append("Attachments: " + ", ".join(item.filename for item in message.attachments))
    return lines


async def selected_messages(context, channel_id, message_ids, *, allow_missing=False):
    if (not 1 <= len(message_ids) <= 100 or len(set(message_ids)) != len(message_ids)
            or any(type(identifier) is not int or identifier < 1 for identifier in message_ids)):
        raise DiscordActionRefused("Choose between 1 and 100 unique messages from one channel.")
    channel = await resolve_channel(context, channel_id)
    guard(context, "view_channel", "read_message_history", channel=channel)
    messages, missing = [], []
    for identifier in message_ids:
        try:
            message = await channel.fetch_message(identifier)
        except discord.NotFound:
            missing.append(identifier)
            continue
        messages.append(message)
    if missing and not allow_missing:
        ids = ", ".join(map(str, missing))
        raise DiscordActionRefused(f"These messages are unavailable in {channel.mention}: {ids}.")
    return channel, messages, missing


def cleanup_access(context, channel, messages):
    required = ("manage_messages",) if any(
        message.author.id != context.member.id for message in messages
    ) else ()
    guard(context, *required, channel=channel, bot_permissions=("manage_messages",))


async def delete_selection(context, channel, messages, reason):
    cleanup_access(context, channel, messages)
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    recent = [message for message in messages if message.created_at > cutoff]
    older = [message for message in messages if message.created_at <= cutoff]
    deleted = []
    if recent:
        try:
            await channel.delete_messages(recent, reason=reason)
            deleted.extend(message.id for message in recent)
        except discord.HTTPException as error:
            LOGGER.warning("Discord message bulk delete failed: channel=%s code=%s",
                           channel.id, error.code)
    for message in older:
        try:
            cleanup_access(context, channel, [message])
            await message.delete()
            deleted.append(message.id)
        except discord.HTTPException as error:
            LOGGER.warning("Discord message delete failed: channel=%s message=%s code=%s",
                           channel.id, message.id, error.code)
    return deleted


async def prepare_cleanup(context, arguments):
    channel, messages, _ = await selected_messages(
        context, arguments["channel_id"], arguments["message_ids"],
    )
    cleanup_access(context, channel, messages)
    before = [message_state(message) for message in messages]
    previous = {message["id"]: message for message in before}

    async def load():
        current, selected, missing = await selected_messages(
            context, channel.id, arguments["message_ids"], allow_missing=True,
        )
        cleanup_access(context, current, selected)
        states = {message.id: message_state(message) for message in selected}
        states.update({identifier: previous[identifier] for identifier in missing})
        return [states[identifier] for identifier in arguments["message_ids"]]

    async def apply(reason):
        current, selected, _ = await selected_messages(
            context, channel.id, arguments["message_ids"], allow_missing=True,
        )
        deleted = await delete_selection(context, current, selected, reason)
        failed = [identifier for identifier in arguments["message_ids"] if identifier not in deleted]
        lines = []
        for label, identifiers in (("Deleted", deleted), ("Not deleted", failed)):
            if identifiers:
                lines.append(f"{label}: {', '.join(map(str, identifiers))}")
        return complete({"deleted": deleted}, text="\n".join(lines),
                        result={"deleted_ids": deleted, "not_deleted_ids": failed})

    return await queue(context, state_action(
        context, "delete_discord_messages", dict(arguments), "Delete messages",
        (f"Delete these messages from {channel.mention}:",), before, load, apply,
        irreversible=True, sources=(channel.id,),
        details=[line for message in messages for line in message_lines(message)],
    ))
