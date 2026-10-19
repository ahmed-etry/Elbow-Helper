"""Confirmed posting and edits limited to agent-owned Discord messages."""

from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import replace
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import discord

from ..plan.executor import resolve_arguments


from ..access import can_access_message_channel, require_evidence_access
from ..actions.contracts import (
    ActionClass, ChangePreview, PreparedAction, earlier_result_label,
)
from ..actions.outcomes import ActionOutcome
from ..models import AgentAttachment, AgentRequestContext
from ..wording import (
    ACTION_ATTACH_LINE,
    ACTION_DELETE_LABEL, ACTION_DELETE_LINE,
    ACTION_EDIT_LABEL, ACTION_EDIT_LINE,
    ACTION_PINGS_LINE, ACTION_POST_FUTURE_LINE,
    ACTION_POST_LABEL, ACTION_POST_LINE,
    ACTION_UNDO_CHANGED, ACTION_PREVIEW_BLANK,
    ACTION_REPLY_NO_PING, ACTION_REPLY_WITH_PING,
)
from .safety import (DiscordActionRefused, check_post_access,
                             check_view_access, resolve_channel)
from .message_parts import outgoing_parts
from .member_labels import member_label, mentioned_member_labels

LOGGER = logging.getLogger(__name__)


def content_options() -> dict[str, Any]:
    return {
        "text": {"type": "string", "minLength": 1, "maxLength": 50_000},
        "ping_everyone": {"type": "boolean"},
        "ping_role_ids": {"type": "array", "items": {"type": "integer", "minimum": 1},
                          "maxItems": 10, "uniqueItems": True},
    }


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


def post_mentions(context: Any, arguments: Mapping[str, Any], text: str) -> discord.AllowedMentions:
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
    return discord.AllowedMentions(
        everyone=everyone, roles=roles, users=True,
        replied_user=bool(arguments.get("ping_author", False)),
    )


def _ping_line(context: Any, arguments: Mapping[str, Any], *,
               text: str | None = None, reply: Any = None,
               member_labels: Mapping[int, str]) -> tuple[str, ...]:
    text = arguments.get("text", "") if text is None else text
    targets = []
    if arguments.get("ping_everyone") and ("@everyone" in text or "@here" in text):
        targets.append("@everyone or @here")
    for role_id in arguments.get("ping_role_ids", ()):
        role = context.guild.get_role(role_id)
        if role is not None and f"<@&{role_id}>" in text:
            targets.append(role.mention)
    members = dict.fromkeys(int(value) for value in re.findall(r"<@!?([0-9]+)>", text))
    if reply is not None and arguments.get("ping_author"):
        members[reply.author.id] = None
    targets.extend(
        member_label(reply.author) if reply is not None and member_id == reply.author.id
        else member_labels[member_id] for member_id in members
    )
    return (ACTION_PINGS_LINE.format(targets=", ".join(targets)),) if targets else ()


async def _channel_for_post(context: AgentRequestContext, channel_id: int) -> Any:
    channel = await resolve_channel(context, channel_id)
    check_post_access(channel, context.member, context.guild.me)
    return channel


async def _reply_target(context: AgentRequestContext, channel: Any, message_id: int) -> Any:
    if not await can_access_message_channel(context, channel):
        raise DiscordActionRefused("That message is unavailable.")
    try:
        message = await channel.fetch_message(message_id)
    except (discord.NotFound, discord.Forbidden) as error:
        raise DiscordActionRefused("That message is unavailable.") from error
    if message.channel.id != channel.id:
        raise DiscordActionRefused("That message is unavailable.")
    return message


def _reply_line(message: Any, *, ping_author: bool) -> str:
    author = member_label(message.author)
    excerpt = " ".join(message.content.split())[:120] or ACTION_PREVIEW_BLANK
    excerpt = discord.utils.escape_markdown(excerpt).replace("]", "\\]")
    wording = ACTION_REPLY_WITH_PING if ping_author else ACTION_REPLY_NO_PING
    return wording.format(member=author, excerpt=excerpt, url=message.jump_url)


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


async def resolve_post_attachment(context, arguments):
    attachment = None
    if "file_name" in arguments:
        if "file_message_id" in arguments:
            try:
                attachment = await _conversation_file(
                    context, arguments["file_message_id"], arguments["file_name"],
                )
            except DiscordActionRefused:
                raise
        else:
            matches = [item for item in context.state.attachments
                       if item.filename == arguments["file_name"]]
            if len(matches) != 1:
                raise DiscordActionRefused("Choose one file made in this conversation.")
            attachment = matches[0]
    elif "file_message_id" in arguments:
        raise DiscordActionRefused("Choose the file name from that agent reply.")
    return attachment


async def prepare_post(context: AgentRequestContext,
                       arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    channel_value = arguments["channel_id"]
    deferred = isinstance(channel_value, Mapping) and set(channel_value) == {"step", "path"}
    try:
        channel = None if deferred else await _channel_for_post(context, channel_value)
        mentions = post_mentions(context, arguments, arguments["text"])
        member_labels = await mentioned_member_labels(context.guild, arguments["text"])
        reply = None
        if "reply_to" in arguments:
            if channel is None:
                raise DiscordActionRefused("That message is unavailable.")
            reply = await _reply_target(context, channel, arguments["reply_to"])
            reply = SimpleNamespace(
                id=reply.id, author=reply.author, content=reply.content, jump_url=reply.jump_url,
            )
    except DiscordActionRefused as error:
        return {"error": str(error)}
    try:
        attachment = await resolve_post_attachment(context, arguments)
    except DiscordActionRefused as error:
        return {"error": str(error)}
    parts = outgoing_parts(arguments["text"])
    if not parts:
        return {"error": "The message has no text."}
    if deferred:
        try:
            target_label = earlier_result_label(context.state.proposed_changes, channel_value)
        except ValueError as error:
            return {"error": str(error)}
    await require_evidence_access(context)
    current = getattr(context, "delivery_channel", None) or context.source_message.channel
    sender = getattr(context, "send_reply", None)
    if (sender is not None and channel is not None and channel.id == current.id
            and "file_name" not in arguments and "file_message_id" not in arguments
            and not arguments.get("ping_everyone") and not arguments.get("ping_role_ids")
            and "@everyone" not in arguments["text"] and "@here" not in arguments["text"]
            and not re.search(r"<@&\d+>", arguments["text"])):
        async with context.state.reply_delivery.lock:
            from ..engine.grounding import GroundingError, ground_answer
            from ..engine.factual_answers import narrow_answer
            try:
                text = narrow_answer(context.state.request_text, context.state.grounding_evidence)
                if text is None:
                    text = ground_answer(arguments["text"], context.state.grounding_evidence)
            except GroundingError as error:
                return {"error": "The answer needs message sources read in this request.",
                        "source_issue": str(error), "missing_result": error.kind,
                        "instruction": "Correct only the unresolved reference or claim, preserving useful supported content."}
            context.state.reply_to = arguments.get("reply_to")
            context.state.ping_author = arguments.get("ping_author", False)
            context.state.source_channels.add(channel.id)
            result = await sender(context, text)
            outcome = {"status": "complete", "delivered_as_reply": True, **result}
            context.state.reply_delivery.posts.append(outcome)
            for part, is_gif in parts:
                if is_gif:
                    context.state.used_gif_urls.add(part)
                    if context.state.gif_url == part:
                        context.state.gif_url = None
            LOGGER.info("Agent reply posted: requester=%s channel=%s messages=%s",
                        context.member.id, channel.id, result["message_ids"])
            return outcome
    actions = []
    for index, (chunk, is_gif) in enumerate(parts):
        make = _deferred_post_part if deferred else _post_part
        actions.append(make(
            context, channel_value if deferred else channel.id, chunk,
            discord.AllowedMentions.none() if is_gif else mentions,
            attachment=attachment if index == 0 else None,
            ping_line=() if is_gif else _ping_line(
                context, arguments, text=chunk, reply=reply if index == 0 else None,
                member_labels=member_labels,
            ),
            **({"reply": reply} if not deferred and index == 0 else {}),
            **({"target_label": target_label} if deferred else {}),
        ))
    if any(is_gif for _, is_gif in parts) and len(actions) > 1:
        context.state.proposed_changes.append(_post_sequence(actions))
    else:
        context.state.proposed_changes.extend(actions)
    return {"status": "confirmation_required"}


def _post_sequence(actions: list[PreparedAction]) -> PreparedAction:
    finished = []

    async def recheck() -> bool:
        return all([await action.preview.recheck() for action in actions])

    async def run() -> ActionOutcome:
        for action in actions:
            finished.append(await action.run())
        identifiers = [item.result["message_id"] for item in finished]
        return ActionOutcome(
            "complete", after={"message_ids": identifiers},
            result={**finished[0].result, "message_ids": identifiers},
            posted_in=finished[0].posted_in,
        )

    async def verify() -> bool:
        return len(finished) == len(actions) and all([
            await action.verify() for action in actions
        ])

    async def bind(results: Mapping[str, Mapping[str, Any]]) -> PreparedAction:
        return _post_sequence([
            await action.bind(results) if action.bind else action for action in actions
        ])

    first = actions[0]
    preview = replace(
        first.preview, recheck=recheck,
        details=tuple(line for action in actions for line in action.preview.details),
    )
    return replace(first, preview=preview, run=run, verify=verify,
                   bind=bind if any(action.bind for action in actions) else None)


def _deferred_post_part(context: AgentRequestContext, reference: Mapping[str, Any],
                        text: str, mentions: discord.AllowedMentions, *,
                        attachment: Any = None,
                        ping_line: tuple[str, ...] = (),
                        target_label: str) -> PreparedAction:
    async def recheck() -> bool:
        return True

    async def unavailable() -> ActionOutcome:
        raise RuntimeError("The earlier action result was not bound")

    async def bind(results: Mapping[str, Mapping[str, Any]]) -> PreparedAction:
        channel_id = resolve_arguments({"channel_id": reference}, results)["channel_id"]
        if type(channel_id) is not int:
            raise DiscordActionRefused("The earlier action did not return a channel.")
        await _channel_for_post(context, channel_id)
        return _post_part(
            context, channel_id, text, mentions,
            attachment=attachment, ping_line=ping_line,
        )

    lines = (ACTION_POST_FUTURE_LINE.format(target=target_label), *ping_line)
    details = tuple(text.splitlines())
    if attachment is not None:
        details += (ACTION_ATTACH_LINE.format(filename=attachment.filename),)
    return PreparedAction(
        "post_discord_message", {"channel_id": dict(reference), "text": text},
        ChangePreview(lines, recheck, summary=ACTION_POST_LABEL,
                      details=details, result_label=target_label),
        unavailable, permission="Send Messages", bind=bind,
    )


def _post_part(context: AgentRequestContext, channel_id: int, text: str,
               mentions: discord.AllowedMentions, *, attachment: Any = None,
               ping_line: tuple[str, ...] = (), reply: Any = None) -> PreparedAction:
    nonce = uuid4().int & ((1 << 63) - 1)
    sent_id: int | None = None

    async def check_reply(channel: Any) -> None:
        if reply is not None:
            current = await _reply_target(context, channel, reply.id)
            if current.author.id != reply.author.id or current.content != reply.content:
                raise DiscordActionRefused("That message is unavailable.")

    async def recheck() -> bool:
        try:
            channel = await _channel_for_post(context, channel_id)
            await check_reply(channel)
        except DiscordActionRefused:
            return False
        return True

    async def record(message: Any) -> None:
        await asyncio.to_thread(
            context.action_repository.record_message,
            message_id=message.id, guild_id=context.guild.id,
            channel_id=channel_id, requester_id=context.member.id,
        )

    async def run() -> ActionOutcome:
        nonlocal sent_id
        channel = await _channel_for_post(context, channel_id)
        await check_reply(channel)
        options = {"allowed_mentions": mentions, "nonce": nonce}
        if reply is not None:
            options["reference"] = discord.MessageReference(
                message_id=reply.id, channel_id=channel_id, guild_id=context.guild.id,
                fail_if_not_exists=True,
            )
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
        return ActionOutcome(
            "complete",
            after={"message_id": sent_id},
            result={"message_id": sent_id, "channel_id": channel_id},
            posted_in=channel_id,
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
    label = channel.mention if channel is not None else f"<#{channel_id}>"
    lines = (ACTION_POST_LINE.format(channel=label), *ping_line)
    details = tuple(text.splitlines())
    if reply is not None:
        details = (_reply_line(reply, ping_author=mentions.replied_user), *details)
    if attachment is not None:
        details += (ACTION_ATTACH_LINE.format(filename=attachment.filename),)
    return PreparedAction(
        "post_discord_message", {"channel_id": channel_id, "text": text, "nonce": nonce},
        ChangePreview(lines, recheck, summary=ACTION_POST_LABEL,
                      details=details, result_label=label,
                      detail_sources=frozenset({channel_id}) if reply is not None else frozenset()),
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
        post_mentions(context, arguments, arguments["text"])
        member_labels = await mentioned_member_labels(context.guild, arguments["text"])
        if len(outgoing_parts(arguments["text"])) > 1:
            await _channel_for_post(context, channel.id)
    except DiscordActionRefused as error:
        return {"error": str(error)}
    if len(arguments["text"]) > 2000:
        return {"error": "One edited message must fit Discord's message limit."}
    context.state.proposed_changes.append(_edit_action(
        context, channel.id, message.id, new_text=arguments["text"],
        old_text=message.content, mention_values=arguments,
        member_labels=member_labels,
    ))
    return {"status": "confirmation_required"}


def _edit_action(context: AgentRequestContext, channel_id: int, message_id: int,
                 *, new_text: str, old_text: str,
                 mention_values: Mapping[str, Any], undo: bool = False,
                 changed: bool = False, remove_gifs: Mapping[str, str] | None = None,
                 member_labels: Mapping[int, str],
                 ) -> PreparedAction:
    parts = outgoing_parts(new_text) or (("", False),)
    primary, primary_is_gif = parts[0]
    companions = [
        _post_part(context, channel_id, text, discord.AllowedMentions.none())
        for text, _ in parts[1:]
    ]
    posted_gifs = {}
    removed_gifs = set()

    async def check_gifs() -> None:
        for identifier, expected in (remove_gifs or {}).items():
            if identifier in removed_gifs:
                continue
            _, message = await _owned_message(context, channel_id, int(identifier))
            if message.content != expected:
                raise DiscordActionRefused("That message is unavailable.")

    async def recheck() -> bool:
        try:
            _, message = await _owned_message(context, channel_id, message_id)
            await check_gifs()
        except DiscordActionRefused:
            return False
        return message.content == old_text and all([
            await companion.preview.recheck() for companion in companions
        ])

    async def run() -> ActionOutcome:
        channel, message = await _owned_message(context, channel_id, message_id)
        await check_gifs()
        mentions = (discord.AllowedMentions.none() if primary_is_gif
                    else post_mentions(context, mention_values, new_text))
        await message.edit(content=primary, allowed_mentions=mentions)
        for identifier in remove_gifs or {}:
            _, companion = await _owned_message(context, channel_id, int(identifier))
            await companion.delete()
            removed_gifs.add(identifier)
            await asyncio.to_thread(
                context.action_repository.mark_message_deleted,
                message_id=int(identifier), guild_id=context.guild.id, channel_id=channel_id,
            )
        for companion in companions:
            outcome = await companion.run()
            posted_gifs[str(outcome.result["message_id"])] = companion.values["text"]
        return ActionOutcome(
            "complete",
            after={"content": primary, "gif_messages": posted_gifs},
            result={"message_id": message_id, "channel_id": channel_id},
        )

    async def verify() -> bool:
        try:
            _, message = await _owned_message(context, channel_id, message_id)
        except DiscordActionRefused:
            return False
        return (message.content == primary and len(posted_gifs) == len(companions)
                and len(removed_gifs) == len(remove_gifs or {})
                and all([await companion.verify() for companion in companions]))

    channel = context.guild.get_channel_or_thread(channel_id)
    label = channel.mention if channel is not None else f"<#{channel_id}>"
    lines = (ACTION_EDIT_LINE.format(channel=label),
             *(() if primary_is_gif else _ping_line(
                 context, mention_values, text=primary, member_labels=member_labels,
             )),
             *((ACTION_UNDO_CHANGED,) if changed else ()))
    return PreparedAction(
        "undo_agent_message_edit" if undo else "edit_agent_message",
        {"channel_id": channel_id, "message_id": message_id, "text": new_text},
        ChangePreview(lines, recheck, summary=ACTION_EDIT_LABEL,
                      details=tuple(new_text.splitlines()),
                      detail_sources=frozenset({channel_id}), before={"content": old_text}),
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
        remove_gifs=log["after"].get("gif_messages", {}),
        member_labels=await mentioned_member_labels(context.guild, previous),
    )


async def prepare_delete(context: AgentRequestContext,
                         arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    await require_evidence_access(context)
    try:
        channel, message = await _owned_message(
            context, arguments["channel_id"], arguments["message_id"],
        )
    except DiscordActionRefused as error:
        return {"error": str(error)}
    before = message.content

    async def recheck() -> bool:
        try:
            _, current = await _owned_message(context, channel.id, message.id)
        except DiscordActionRefused:
            return False
        return current.content == before

    async def run() -> ActionOutcome:
        _, current = await _owned_message(context, channel.id, message.id)
        await current.delete()
        await asyncio.to_thread(
            context.action_repository.mark_message_deleted,
            message_id=message.id, guild_id=context.guild.id, channel_id=channel.id,
        )
        return ActionOutcome("complete", after={"deleted": True})

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

    lines = (ACTION_DELETE_LINE.format(channel=channel.mention),)
    context.state.proposed_changes.append(PreparedAction(
        "delete_agent_message", dict(arguments),
        ChangePreview(lines, recheck, summary=ACTION_DELETE_LABEL,
                      details=tuple(before.splitlines() or (ACTION_PREVIEW_BLANK,)),
                      detail_sources=frozenset({channel.id}), before={"content": before}),
        run, ActionClass.IRREVERSIBLE, verify=verify,
        permission="Manage Messages",
    ))
    return {"status": "confirmation_required"}


UNDO_HANDLERS = {
    'edit_agent_message': prepare_edit_undo,
    'undo_agent_message_edit': prepare_edit_undo,
}
