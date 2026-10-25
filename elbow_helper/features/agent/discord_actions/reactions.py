"""Request-message reactions without a confirmation preview."""

import re

import aiohttp
import discord
import emoji as emoji_library

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..actions.contracts import ActionClass
from ..engine.capability_contract import CapabilityContract
from ..models import RegisteredAgentTool
from .reaction_meanings import REACTION_MEANINGS


_TEXT_EMOJI = re.compile(
    r"<a?:\w+:\d+>|https?://\S+|```[\s\S]*?```|`[^`\n]*`|:([A-Za-z0-9_]+):"
    r"|(?<![\w:/]):([A-Za-z0-9_]+)(?=$|[\s.!?,;])"
)


def render_server_emojis(text, guild):
    """Resolve known shortcodes without rewriting markup, URLs or code."""
    available = {item.name.casefold(): str(item) for item in getattr(guild, "emojis", ())
                 if item.is_usable()}
    rendered = _TEXT_EMOJI.sub(
        lambda match: available.get((match.group(1) or match.group(2) or "").casefold(), match.group(0)), text,
    )
    # A trailing social emoji belongs beside its sentence. Preserve code,
    # quotations, tables and lists, where a line break carries structure.
    body, separator, tail = rendered.rstrip().rpartition("\n")
    body = body.rstrip()
    tokens = tail.split()
    if separator and body and tokens and all(token in available.values() for token in tokens):
        last_line = body.splitlines()[-1].lstrip()
        if (not body.count("`") % 2 and not body.count("~~~") % 2
                and not last_line.startswith(("```", "~~~", ">", "|", "#", "- ", "* ", "+ "))
                and not re.match(r"\d+[.)]\s", last_line)):
            return body + " " + " ".join(tokens)
    return rendered


def server_reaction_context(guild):
    """Pair usable markup with intent; never ask the model to guess an unknown meaning."""
    emojis = [
        f"{item}: {REACTION_MEANINGS[item.name.casefold()]}"
        for item in getattr(guild, "emojis", ())
        if item.is_usable() and item.name.casefold() in REACTION_MEANINGS
    ]
    return (
        "Available server reaction emojis and meanings:\n" + "\n".join(emojis)
        + "\nChoose only a meaning that fits the message. No fitting meaning means no reaction."
    ) if emojis else ""


def resolve_reaction(value, guild):
    """Resolve one selection against the current usable Discord cache."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    custom = re.fullmatch(r"<a?:\w+:(\d+)>", value)
    if custom:
        item = guild.get_emoji(int(custom.group(1))) if guild is not None else None
        return item if item is not None and item.is_usable() else None
    name = value.strip(":").casefold()
    matches = [item for item in getattr(guild, "emojis", ())
               if item.name.casefold() == name and item.is_usable()]
    if len(matches) == 1:
        return matches[0]
    if emoji_library.is_emoji(value):
        return value
    resolved = emoji_library.emojize(value, language="alias")
    return resolved if emoji_library.is_emoji(resolved) else None


def _failure(cause, message):
    return {"error": message, "cause": cause, "reacted": False,
            "flags": {"status": "failed", "limits": [cause]}}


def channel_reaction_history(messages):
    """Read this bot's reaction metadata, never member-written content."""
    for message in reversed(messages):
        emojis = tuple(str(item.emoji) for item in getattr(message, "reactions", ())
                       if getattr(item, "me", False))
        if emojis:
            return message.id, emojis
    return 0, ()


def reaction_key(value):
    """Ignore custom emoji renames and Unicode presentation selectors."""
    identifier = getattr(value, "id", None)
    text = str(value)
    if identifier is None and text.startswith("<") and text.endswith(">"):
        tail = text[:-1].rpartition(":")[2]
        identifier = int(tail) if tail.isdecimal() else None
    return "custom:" + str(identifier) if identifier is not None else text.replace("\ufe0f", "").replace("\ufe0e", "")


def recent_reaction_context(context):
    emojis = context.state.channel_reactions.emojis
    if emojis is None:
        return "Recent bot reactions are unavailable in this channel. Leave reactions empty."
    return ("Previous bot reaction in this channel: " + " ".join(emojis)
            + ". Choose different emojis or no reaction. Every reaction needs a line or a GIF.") if emojis else ""


async def _add_reaction(context, selection, previous):
    """Add one reaction while the caller holds the request's reaction lock."""
    await require_evidence_access(context)
    value = resolve_reaction(selection, getattr(context, "guild", None))
    if value is None:
        return _failure("invalid_selection", "That emoji is unavailable.")
    tracker = context.state.reactions
    if tracker.count >= (3 if context.state.reaction_combo_due else 1):
        return _failure("reaction_limit", "The reaction limit has been reached.")
    if reaction_key(value) in previous or any(reaction_key(value) == reaction_key(item) for item in tracker.emojis):
        return _failure("repeated_reaction", "Choose a different emoji from the previous channel reaction.")
    try:
        await context.source_message.add_reaction(value)
    except discord.Forbidden:
        return _failure("permission_denied", "I can't add a reaction to that message.")
    except discord.HTTPException as error:
        if error.status == 403:
            return _failure("permission_denied", "I can't add a reaction to that message.")
        return _failure("delivery_failed", "I couldn't add that reaction.")
    except (aiohttp.ClientError, OSError, TimeoutError):
        return _failure("delivery_failed", "I couldn't add that reaction.")
    tracker.count += 1
    tracker.emojis.append(str(value))
    return {"reacted": True}


async def react_to_request(context, arguments):
    message = context.source_message
    if not getattr(message, "id", None) or not callable(getattr(message, "add_reaction", None)):
        return _failure("missing_message", "There is no message to react to.")
    selection = arguments["emoji"]
    selections = [selection] if isinstance(selection, str) else selection
    if not isinstance(selections, list) or not 1 <= len(selections) <= 3:
        return _failure("invalid_selection", "Choose one to three reactions in order.")
    async with context.state.reactions.lock:
        channel = context.state.channel_reactions
        async with channel.lock:
            if channel.emojis is None:
                return _failure("history_unavailable", "Recent channel reactions are unavailable. Use a line or a GIF.")
            if len(selections) + context.state.reactions.count > 1 and (
                not context.state.reaction_combo_due
                or context.state.reaction_combo_revision != channel.combo_revision
            ):
                return _failure("combo_not_offered", "No combo slot is available. Use a line or a single reaction.")
            previous = {reaction_key(item) for item in channel.emojis}
            first = len(context.state.reactions.emojis)
            try:
                results = [await _add_reaction(context, item, previous) for item in selections]
            finally:
                successful = context.state.reactions.emojis[first:]
                if successful:
                    channel.emojis = tuple(successful)
                    channel.message_id = max(channel.message_id, message.id)
                    if len(successful) >= 2:
                        channel.combo_message_id = max(channel.combo_message_id, message.id)
                        channel.combo_revision += 1
    if isinstance(selection, str):
        return results[0]
    failed = sum(not result["reacted"] for result in results)
    return {
        "reacted": failed < len(results), "results": results,
        "flags": {"status": "complete" if not failed else "failed" if failed == len(results) else "partial"},
    }


def reaction_tools():
    return (RegisteredAgentTool(
        AgentToolDefinition(
            "react_to_request",
            (
                "React with the expression that fits this exchange, using one or more distinct emojis."
            ),
            {
                "type": "object", "properties": {"emoji": {"anyOf": [
                    {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3},
                    {"type": "string"},
                ]}},
                "required": ["emoji"], "additionalProperties": False,
            },
            usage_note="Ground each emoji in the same point; judge the whole response. Combinations are optional when offered. Avoid previous channel emojis. Add a line or GIF.",
        ),
        react_to_request, action_class=ActionClass.OUTPUT,
        contract=CapabilityContract(source_scope="request_context"),
    ),)
