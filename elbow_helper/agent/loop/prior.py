"""The conversation before this request, replayed to the model as its own earlier turns."""
from __future__ import annotations

from dataclasses import replace
import json

import discord

from elbow_helper.agent.conversation.own_output import action_turn_text
from elbow_helper.agent.conversation.reply_context import prepare_reply_context, turn_identity
from elbow_helper.agent.conversation.text import message_text
from elbow_helper.agent.discord_actions.gif_library import describe_sent_gifs, own_message_text
from elbow_helper.agent.safety.access import accessible_message_channel, has_lookup_access, require_evidence_access


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def resolve_reference(message):
    """The message this one replies to, only from the same channel."""
    reference = message.reference
    if reference is None or reference.message_id is None:
        return None
    if reference.channel_id is not None and reference.channel_id != message.channel.id:
        return None
    if isinstance(reference.resolved, discord.Message):
        resolved = reference.resolved
        if (getattr(getattr(resolved, "guild", None), "id", None) == message.guild.id
                and getattr(getattr(resolved, "channel", None), "id", None) == message.channel.id):
            return resolved
        return None
    try:
        return await message.channel.fetch_message(reference.message_id)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        return None


async def prior_messages(context, referenced, resolve_reference):
    await require_evidence_access(context)
    state = context.state
    authorized = state.authorized_history or ()
    prior = []
    for turn in authorized:
        record = turn.record
        data = json.loads(turn.text) if record is None else {}
        if (action := action_turn_text(data)) is not None:
            # A confirmed change's outcome, or a Confirm card that closed without running.
            prior.append({"role": "assistant", "content": action})
        question = record.question if record else data.get("question")
        answer = record.delivered_answer if record else data.get("answer")
        request_id, reply_ids = turn_identity(turn)
        if question:
            prior.append({"role": "user", "content": question})
        for index, encoded in enumerate(record.lookup_results if record else data.get("lookup_results", ())):
            lookup = json.loads(encoded) if isinstance(encoded, str) else encoded
            requirements = frozenset(lookup["required_access"])
            sources = frozenset(lookup["source_channels"])
            if not has_lookup_access(context, requirements):
                continue
            if any([await accessible_message_channel(context, channel) is None for channel in sources]):
                continue
            state.required_access.update(requirements)
            state.source_channels.update(sources)
            state.knowledge_sources.update(lookup.get("knowledge_sources", ()))
            await require_evidence_access(context)
            call_id = f"prior_{request_id}_{index}"
            prior.extend((
                {"type": "function_call", "call_id": call_id, "name": lookup["capability"],
                 "arguments": _json(lookup["arguments"])},
                {"type": "function_call_output", "call_id": call_id, "output": lookup["output"]},
            ))
        if answer:
            prior.append({"role": "assistant", "content": describe_sent_gifs(answer)})
        state.source_channels.update(turn.source_channels)
        state.required_access.update(turn.required_access)

    known = set()
    for turn in context.history:
        request_id, reply_ids = turn_identity(turn)
        known.update((request_id, *reply_ids))
    allowed = set()
    for turn in authorized:
        request_id, reply_ids = turn_identity(turn)
        allowed.update((request_id, *reply_ids))
    if referenced is not None and referenced.id in known - allowed:
        context = replace(context, attachment_sources=tuple(
            message for message in context.attachment_sources if message.id != referenced.id))
    await prepare_reply_context(context)

    channel_id = context.source_message.channel.id
    linked = []
    if referenced is not None and await accessible_message_channel(context, channel_id) is not None:
        seen = known | {context.source_message.id}
        item = referenced
        while item is not None and item.id not in seen:
            seen.add(item.id)
            if item.channel.id != channel_id:
                break
            role = "assistant" if item.author.id == context.guild.me.id else "user"
            content = own_message_text(item) if role == "assistant" else message_text(item)
            row = {"message_id": item.id, "author_id": item.author.id,
                   "author": item.author.display_name, "content": content, "source": item.jump_url,
                   "timestamp": item.created_at.isoformat()}
            linked.append((row, {"role": role, "content": f"{row['author']} (member_id={row['author_id']}): {content}"}))
            item = await resolve_reference(item)
    state.local_messages = [row for row, _ in reversed(linked)]
    prior.extend(message for _, message in reversed(linked))
    return context, prior


def last_result_id(prior):
    last = 0
    for message in prior:
        if message.get("type") != "function_call_output":
            continue
        try:
            page = json.loads(message["output"]).get("result_page")
            last = max(last, int(page["result_id"]))
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
    return last
