"""Confirmed posting and edits limited to agent-owned Discord messages."""

from __future__ import annotations

import asyncio
import io
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

import discord

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..actions.contracts import (
    ActionClass, ChangePreview, PreparedAction, earlier_result_label,
)
from ..actions.outcomes import CommandOutcome
from ..message_parts import chunk_response
from ..models import AgentAttachment, AgentCapabilityEffect, AgentRequestContext, RegisteredAgentTool
from ..wording import (
    ACTION_ATTACH_LINE,
    ACTION_DELETE_LABEL, ACTION_DELETE_LINE,
    ACTION_EDIT_LABEL, ACTION_EDIT_LINE,
    ACTION_PINGS_LINE, ACTION_POST_FUTURE_LINE,
    ACTION_POST_LABEL, ACTION_POST_LINE,
    ACTION_UNDO_CHANGED,
)
from .safety import (DiscordActionRefused, check_post_access,
                             check_view_access, resolve_channel)


def _content_options() -> dict[str, Any]:
    return {
        "text": {"type": "string", "minLength": 1, "maxLength": 50_000},
        "ping_everyone": {"type": "boolean"},
        "ping_role_ids": {"type": "array", "items": {"type": "integer", "minimum": 1},
                          "maxItems": 10, "uniqueItems": True},
    }


def discord_message_tools() -> tuple[RegisteredAgentTool, ...]:
    return (
        RegisteredAgentTool(AgentToolDefinition(
            name="find_agent_files",
            description="List files the agent delivered earlier in this conversation. Returns their filename and reply message ID for posting a selected file.",
            parameters={"type": "object", "properties": {
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            }, "required": [], "additionalProperties": False},
        ), find_agent_files),
        RegisteredAgentTool(AgentToolDefinition(
            name="post_discord_message",
            description="Post text in a channel visible and writable by both the asker and bot; long text is split. Can attach a file made in this conversation. Pings require explicit preview values. Returns each posted message ID.",
            parameters={"type": "object", "properties": {
                "channel_id": {"type": "integer", "minimum": 1},
                **_content_options(),
                "file_name": {"type": "string", "minLength": 1, "maxLength": 255},
                "file_message_id": {"type": "integer", "minimum": 1},
            }, "required": ["channel_id", "text"], "additionalProperties": False},
        ), prepare_post, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE, True),
        RegisteredAgentTool(AgentToolDefinition(
            name="edit_agent_message",
            description="Edit text in a message posted by post_discord_message. Pings require explicit preview values.",
            parameters={"type": "object", "properties": {
                "channel_id": {"type": "integer", "minimum": 1},
                "message_id": {"type": "integer", "minimum": 1},
                **_content_options(),
            }, "required": ["channel_id", "message_id", "text"],
               "additionalProperties": False},
        ), prepare_edit, AgentCapabilityEffect.COMMAND, ActionClass.CHANGE, True),
        RegisteredAgentTool(AgentToolDefinition(
            name="delete_agent_message",
            description="Delete one message posted by post_discord_message. This cannot be undone.",
            parameters={"type": "object", "properties": {
                "channel_id": {"type": "integer", "minimum": 1},
                "message_id": {"type": "integer", "minimum": 1},
            }, "required": ["channel_id", "message_id"],
               "additionalProperties": False},
        ), prepare_delete, AgentCapabilityEffect.COMMAND, ActionClass.IRREVERSIBLE, True),
    )


def _conversation_reply_ids(context: AgentRequestContext) -> tuple[int, ...]:
    return tuple(dict.fromkeys(
        message_id
        for turn in reversed(context.history)
        if turn.record is not None
        for message_id in reversed(turn.record.reply_ids)
    ))


async def _conversation_file(context: AgentRequestContext, message_id: int,
                             filename: str) -> AgentAttachment:
    if message_id not in _conversation_reply_ids(context):
        raise DiscordActionRefused("That file is not from this conversation.")
    channel = context.source_message.channel
    check_view_access(channel, context.member, context.guild.me)
    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound as error:
        raise DiscordActionRefused("That file is no longer available.") from error
    if message.author.id != context.guild.me.id:
        raise DiscordActionRefused("That file is not from an agent reply.")
    matches = [item for item in message.attachments if item.filename == filename]
    if len(matches) != 1:
        raise DiscordActionRefused("Choose one file from that agent reply.")
    attachment = matches[0]
    if attachment.size > 24 * 1024 * 1024:
        raise DiscordActionRefused("That file is too large to post here.")
    data = await attachment.read(use_cached=False)
    return AgentAttachment(filename, data)


async def find_agent_files(context: AgentRequestContext,
                           arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    channel = context.source_message.channel
    check_view_access(channel, context.member, context.guild.me)
    reply_ids = _conversation_reply_ids(context)
    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", 25)
    files = []
    for message_id in reply_ids[offset:offset + limit]:
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            continue
        if message.author.id != context.guild.me.id:
            continue
        files.extend({
            "message_id": message_id, "file_name": item.filename, "size": item.size,
        } for item in message.attachments)
    await require_evidence_access(context)
    return {"files": files, "next_offset": offset + limit if offset + limit < len(reply_ids) else None}


def _mentions(context: Any, arguments: Mapping[str, Any], text: str) -> discord.AllowedMentions:
    roles = []
    for role_id in arguments.get("ping_role_ids", ()):
        if f"<@&{role_id}>" not in text:
            raise DiscordActionRefused("A selected role ping is missing from the message.")
        role = context.guild.get_role(role_id)
        if role is None:
            raise DiscordActionRefused("A selected role ping is unavailable.")
        roles.append(role)
    everyone = bool(arguments.get("ping_everyone", False))
    if everyone and "@everyone" not in text and "@here" not in text:
        raise DiscordActionRefused("The selected everyone ping is missing from the message.")
    return discord.AllowedMentions(everyone=everyone, roles=roles, users=True)


def _ping_line(context: Any, arguments: Mapping[str, Any]) -> tuple[str, ...]:
    targets = []
    if arguments.get("ping_everyone"):
        targets.append("@everyone or @here")
    for role_id in arguments.get("ping_role_ids", ()):
        role = context.guild.get_role(role_id)
        if role is not None:
            targets.append(role.mention)
    return (ACTION_PINGS_LINE.format(targets=", ".join(targets)),) if targets else ()


async def _channel_for_post(context: AgentRequestContext, channel_id: int) -> Any:
    channel = await resolve_channel(context, channel_id)
    check_post_access(channel, context.member, context.guild.me)
    return channel


async def _find_nonce(channel: Any, nonce: int, bot_id: int) -> Any | None:
    history = getattr(channel, "history", None)
    if not callable(history):
        return None
    async with asyncio.timeout(5):
        async for message in history(limit=25):
            if (getattr(getattr(message, "author", None), "id", None) == bot_id
                    and str(getattr(message, "nonce", "")) == str(nonce)):
                return message
    return None


async def prepare_post(context: AgentRequestContext,
                       arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    channel_value = arguments["channel_id"]
    deferred = isinstance(channel_value, Mapping) and set(channel_value) == {"step", "path"}
    try:
        channel = None if deferred else await _channel_for_post(context, channel_value)
        mentions = _mentions(context, arguments, arguments["text"])
    except DiscordActionRefused as error:
        return {"error": str(error), "prepared_count": 0}
    attachment = None
    if "file_name" in arguments:
        if "file_message_id" in arguments:
            try:
                attachment = await _conversation_file(
                    context, arguments["file_message_id"], arguments["file_name"],
                )
            except DiscordActionRefused as error:
                return {"error": str(error), "prepared_count": 0}
        else:
            matches = [item for item in context.state.attachments
                       if item.filename == arguments["file_name"]]
            if len(matches) != 1:
                return {"error": "Choose one file made in this conversation.",
                        "prepared_count": 0}
            attachment = matches[0]
    elif "file_message_id" in arguments:
        return {"error": "Choose the file name from that agent reply.",
                "prepared_count": 0}
    chunks = chunk_response(arguments["text"])
    if not chunks:
        return {"error": "The message has no text.", "prepared_count": 0}
    if deferred:
        try:
            target_label = earlier_result_label(context.state.command_proposals, channel_value)
        except ValueError as error:
            return {"error": str(error), "prepared_count": 0}
    await require_evidence_access(context)
    for index, chunk in enumerate(chunks):
        make = _deferred_post_part if deferred else _post_part
        context.state.command_proposals.append(make(
            context, channel_value if deferred else channel.id, chunk, mentions,
            attachment=attachment if index == 0 else None,
            ping_line=_ping_line(context, arguments),
            **({"target_label": target_label} if deferred else {}),
        ))
    return {"status": "confirmation_required", "prepared_count": len(chunks)}


def _deferred_post_part(context: AgentRequestContext, reference: Mapping[str, Any],
                        text: str, mentions: discord.AllowedMentions, *,
                        attachment: Any = None,
                        ping_line: tuple[str, ...] = (),
                        target_label: str) -> PreparedAction:
    async def recheck() -> bool:
        return True

    async def unavailable() -> CommandOutcome:
        raise RuntimeError("The earlier action result was not bound")

    async def bind(results: Mapping[str, Mapping[str, Any]]) -> PreparedAction:
        from ..plan.executor import resolve_arguments
        channel_id = resolve_arguments({"channel_id": reference}, results)["channel_id"]
        if type(channel_id) is not int:
            raise DiscordActionRefused("The earlier action did not return a channel.")
        await _channel_for_post(context, channel_id)
        return _post_part(
            context, channel_id, text, mentions,
            attachment=attachment, ping_line=ping_line,
        )

    lines = (ACTION_POST_FUTURE_LINE.format(target=target_label),
             *text.splitlines(), *ping_line)
    if attachment is not None:
        lines += (ACTION_ATTACH_LINE.format(filename=attachment.filename),)
    return PreparedAction(
        "post_discord_message", {"channel_id": dict(reference), "text": text},
        ChangePreview(lines, recheck, summary=ACTION_POST_LABEL,
                      result_label=target_label),
        unavailable, permission="Send Messages", bind=bind,
    )


def _post_part(context: AgentRequestContext, channel_id: int, text: str,
               mentions: discord.AllowedMentions, *, attachment: Any = None,
               ping_line: tuple[str, ...] = ()) -> PreparedAction:
    nonce = uuid4().int & ((1 << 63) - 1)
    sent_id: int | None = None

    async def recheck() -> bool:
        try:
            await _channel_for_post(context, channel_id)
        except DiscordActionRefused:
            return False
        return True

    async def record(message: Any) -> None:
        await asyncio.to_thread(
            context.action_repository.record_message,
            message_id=message.id, guild_id=context.guild.id,
            channel_id=channel_id, requester_id=context.member.id,
        )

    async def run() -> CommandOutcome:
        nonlocal sent_id
        channel = await _channel_for_post(context, channel_id)
        options = {"allowed_mentions": mentions, "nonce": nonce}
        file = (discord.File(io.BytesIO(attachment.data), filename=attachment.filename)
                if attachment is not None else None)
        try:
            if file is not None:
                options["file"] = file
            try:
                message = await channel.send(text, **options)
            except (OSError, TimeoutError, discord.HTTPException) as error:
                if isinstance(error, discord.HTTPException) and error.status < 500:
                    raise
                message = await _find_nonce(channel, nonce, context.guild.me.id)
                if message is None:
                    raise
            sent_id = message.id
            await record(message)
        finally:
            if file is not None:
                file.close()
        return CommandOutcome(
            "complete",
            after={"message_id": sent_id},
            result={"message_id": sent_id, "channel_id": channel_id},
        )

    async def verify() -> bool:
        channel = await resolve_channel(context, channel_id)
        message = (await channel.fetch_message(sent_id) if sent_id is not None
                   else await _find_nonce(channel, nonce, context.guild.me.id))
        if message is None:
            return False
        await record(message)
        return True

    channel = context.guild.get_channel_or_thread(channel_id)
    label = channel.mention if channel is not None else f"channel {channel_id}"
    lines = (ACTION_POST_LINE.format(channel=label), *text.splitlines(), *ping_line)
    if attachment is not None:
        lines += (ACTION_ATTACH_LINE.format(filename=attachment.filename),)
    return PreparedAction(
        "post_discord_message", {"channel_id": channel_id, "text": text, "nonce": nonce},
        ChangePreview(lines, recheck, summary=ACTION_POST_LABEL,
                      result_label=label),
        run, verify=verify, permission="Send Messages",
    )


async def _owned_message(context: AgentRequestContext, channel_id: int,
                         message_id: int) -> tuple[Any, Any]:
    channel = await resolve_channel(context, channel_id)
    check_view_access(channel, context.member, context.guild.me)
    owned = await asyncio.to_thread(
        context.action_repository.agent_message,
        message_id=message_id, guild_id=context.guild.id, channel_id=channel_id,
    )
    if owned is None:
        raise DiscordActionRefused("That message was not posted with this action.")
    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound as error:
        raise DiscordActionRefused("That message is no longer available.") from error
    if message.author.id != context.guild.me.id:
        raise DiscordActionRefused("That message is no longer an agent post.")
    return channel, message


async def prepare_edit(context: AgentRequestContext,
                       arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    try:
        channel, message = await _owned_message(
            context, arguments["channel_id"], arguments["message_id"],
        )
        _mentions(context, arguments, arguments["text"])
    except DiscordActionRefused as error:
        return {"error": str(error), "prepared_count": 0}
    if len(arguments["text"]) > 2000:
        return {"error": "One edited message must fit Discord's message limit.",
                "prepared_count": 0}
    context.state.command_proposals.append(_edit_action(
        context, channel.id, message.id, new_text=arguments["text"],
        old_text=message.content, mention_values=arguments,
    ))
    return {"status": "confirmation_required"}


def _edit_action(context: AgentRequestContext, channel_id: int, message_id: int,
                 *, new_text: str, old_text: str,
                 mention_values: Mapping[str, Any], undo: bool = False,
                 changed: bool = False) -> PreparedAction:
    async def recheck() -> bool:
        try:
            _, message = await _owned_message(context, channel_id, message_id)
        except DiscordActionRefused:
            return False
        return message.content == old_text

    async def run() -> CommandOutcome:
        channel, message = await _owned_message(context, channel_id, message_id)
        await message.edit(content=new_text,
                           allowed_mentions=_mentions(context, mention_values, new_text))
        return CommandOutcome(
            "complete",
            after={"content": new_text},
            result={"message_id": message_id, "channel_id": channel_id},
        )

    async def verify() -> bool:
        try:
            _, message = await _owned_message(context, channel_id, message_id)
        except DiscordActionRefused:
            return False
        return message.content == new_text

    channel = context.guild.get_channel_or_thread(channel_id)
    label = channel.mention if channel is not None else f"channel {channel_id}"
    lines = (ACTION_EDIT_LINE.format(channel=label), *new_text.splitlines(),
             *_ping_line(context, mention_values),
             *((ACTION_UNDO_CHANGED,) if changed else ()))
    return PreparedAction(
        "undo_agent_message_edit" if undo else "edit_agent_message",
        {"channel_id": channel_id, "message_id": message_id, "text": new_text},
        ChangePreview(lines, recheck, summary=ACTION_EDIT_LABEL,
                      before={"content": old_text}),
        run, verify=verify, permission="Send Messages",
    )


async def prepare_edit_undo(context: AgentRequestContext,
                            log: Mapping[str, Any]) -> PreparedAction:
    values = log["targets"]
    channel, message = await _owned_message(
        context, values["channel_id"], values["message_id"],
    )
    previous = log["before"]["content"]
    expected = log["after"]["content"]
    return _edit_action(
        context, channel.id, message.id, new_text=previous,
        old_text=expected, mention_values={}, undo=True,
        changed=message.content != expected,
    )


async def prepare_delete(context: AgentRequestContext,
                         arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    try:
        channel, message = await _owned_message(
            context, arguments["channel_id"], arguments["message_id"],
        )
    except DiscordActionRefused as error:
        return {"error": str(error), "prepared_count": 0}
    before = message.content

    async def recheck() -> bool:
        try:
            _, current = await _owned_message(context, channel.id, message.id)
        except DiscordActionRefused:
            return False
        return current.content == before

    async def run() -> CommandOutcome:
        _, current = await _owned_message(context, channel.id, message.id)
        await current.delete()
        await asyncio.to_thread(
            context.action_repository.mark_message_deleted,
            message_id=message.id, guild_id=context.guild.id, channel_id=channel.id,
        )
        return CommandOutcome("complete", after={"deleted": True})

    async def verify() -> bool:
        try:
            await channel.fetch_message(message.id)
        except discord.NotFound:
            await asyncio.to_thread(
                context.action_repository.mark_message_deleted,
                message_id=message.id, guild_id=context.guild.id, channel_id=channel.id,
            )
            return True
        return False

    lines = (ACTION_DELETE_LINE.format(channel=channel.mention),
             *(before.splitlines() or ("-",)))
    context.state.command_proposals.append(PreparedAction(
        "delete_agent_message", dict(arguments),
        ChangePreview(lines, recheck, summary=ACTION_DELETE_LABEL,
                      before={"content": before}),
        run, ActionClass.IRREVERSIBLE, verify=verify,
        permission="Manage Messages",
    ))
    return {"status": "confirmation_required"}
