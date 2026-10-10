"""Repost exactly reviewed messages, preserving attribution and files before deletion."""

import logging

import discord

from ..actions.values import display_value
from .management import ID, complete, guard, queue, state_action, tool
from .message_cleanup import (
    MESSAGE_IDS, delete_selection, message_lines, message_state, selected_messages,
)
from .safety import DiscordActionRefused, resolve_channel


LOGGER = logging.getLogger(__name__)


def message_move_tools():
    return (tool(
        "move_discord_messages", "Repost reviewed messages and attachments, then delete originals.",
        {"source_channel_id": ID, "target_channel_id": ID, "message_ids": MESSAGE_IDS},
        ("source_channel_id", "target_channel_id", "message_ids"), prepare_move,
        irreversible=True,
        entities=(("source_channel_id", "discord_channel"),
                  ("target_channel_id", "discord_channel"), ("message_ids", "discord_message_set")),
    ),)


async def move_access(context, source, target_id, messages):
    if source.id == target_id:
        raise DiscordActionRefused("Choose a different destination channel.")
    guard(context, "view_channel", "read_message_history", "manage_messages", channel=source)
    target = await resolve_channel(context, target_id)
    if not callable(getattr(target, "send", None)):
        raise DiscordActionRefused("The selected destination channel is invalid.")
    permission = ("send_messages_in_threads" if isinstance(target, discord.Thread)
                  else "send_messages")
    required = ("view_channel", permission)
    if any(message.attachments for message in messages):
        required += ("attach_files",)
    guard(context, *required, channel=target)
    too_large = [item.filename for message in messages for item in message.attachments
                 if item.size > context.guild.filesize_limit]
    if too_large:
        raise DiscordActionRefused(
            "These attachments exceed this server's file size limit: " + ", ".join(too_large) + "."
        )
    return target


async def prepare_move(context, arguments):
    source, messages, _ = await selected_messages(
        context, arguments["source_channel_id"], arguments["message_ids"],
    )
    target = await move_access(context, source, arguments["target_channel_id"], messages)
    before = [message_state(message) for message in messages]
    previous = {row["id"]: row for row in before}

    async def load():
        current, selected, missing = await selected_messages(
            context, source.id, arguments["message_ids"], allow_missing=True,
        )
        await move_access(context, current, target.id, selected)
        states = {message.id: message_state(message) for message in selected}
        states.update({identifier: previous[identifier] for identifier in missing})
        return [states[identifier] for identifier in arguments["message_ids"]]

    async def apply(reason):
        current, selected, _ = await selected_messages(
            context, source.id, arguments["message_ids"], allow_missing=True,
        )
        copied, reposted, posted_ids = [], set(), []
        for message in selected:
            files = []
            try:
                destination = await move_access(context, current, target.id, [message])
                for attachment in message.attachments:
                    files.append(await attachment.to_file())
                destination = await move_access(context, current, target.id, [message])
                author = discord.utils.escape_markdown(message.author.display_name)
                content = f"**{author}** ({display_value(message.created_at)})\n{message.content}"
                parts = [content[index:index + 2000] for index in range(0, len(content), 2000)]
                for index, part in enumerate(parts):
                    destination = await move_access(context, current, target.id, [message])
                    posted = await destination.send(
                        part, files=files if index == 0 else [],
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    posted_ids.append(posted.id)
                    reposted.add(message.id)
                copied.append(message)
            except discord.HTTPException as error:
                LOGGER.warning("Discord message move failed: source=%s target=%s "
                               "message=%s code=%s",
                               source.id, target.id, message.id, error.code)
            except DiscordActionRefused:
                break
            finally:
                for file in files:
                    file.close()
        deleted = []
        if copied:
            try:
                await move_access(context, current, target.id, copied)
                deleted = await delete_selection(context, current, copied, reason)
            except DiscordActionRefused:
                pass
        moved = [identifier for identifier in arguments["message_ids"] if identifier in deleted]
        failed = [identifier for identifier in arguments["message_ids"] if identifier not in moved]
        retained = [identifier for identifier in failed if identifier in reposted]
        lines = []
        for label, identifiers in (("Moved", moved), ("Not moved", failed),
                                   ("Reposted but not deleted", retained)):
            if identifiers:
                lines.append(f"{label}: {', '.join(map(str, identifiers))}")
        result = {"moved_ids": moved, "not_moved_ids": failed,
                  "reposted_but_not_deleted_ids": retained, "posted_message_ids": posted_ids}
        return complete(result, text="\n".join(lines), result=result)

    return await queue(context, state_action(
        context, "move_discord_messages", dict(arguments), "Move messages",
        (f"Move these messages from {source.mention} to {target.mention}, "
         "then delete the originals:",), before, load, apply,
        irreversible=True, sources=(source.id,),
        details=[line for message in messages for line in message_lines(message)],
    ))
