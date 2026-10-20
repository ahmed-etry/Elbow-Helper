"""Request-message reactions without a confirmation preview."""

import re

import discord
import emoji as emoji_library

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..actions.contracts import ActionClass
from ..engine.capability_contract import CapabilityContract
from ..models import RegisteredAgentTool


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
    """Expose names and exact markup from Discord's cached usable server emojis."""
    emojis = [str(item) for item in getattr(guild, "emojis", ()) if item.is_usable()]
    return "Available server reaction emojis: " + " ".join(emojis) if emojis else ""


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


async def react_to_request(context, arguments):
    message = context.source_message
    if not getattr(message, "id", None) or not callable(getattr(message, "add_reaction", None)):
        return _failure("missing_message", "There is no message to react to.")
    await require_evidence_access(context)
    value = resolve_reaction(arguments["emoji"], getattr(context, "guild", None))
    if value is None:
        return _failure("invalid_selection", "That emoji is unavailable.")
    tracker = context.state.reactions
    async with tracker.lock:
        if tracker.count >= 3:
            return _failure("reaction_limit", "The reaction limit has been reached.")
        try:
            await message.add_reaction(value)
        except discord.Forbidden:
            return _failure("permission_denied", "I can't add a reaction to that message.")
        except discord.HTTPException as error:
            if error.status == 403:
                return _failure("permission_denied", "I can't add a reaction to that message.")
            return _failure("delivery_failed", "I couldn't add that reaction.")
        tracker.count += 1
        tracker.emojis.append(str(value))
    return {"reacted": True}


def reaction_tools():
    return (RegisteredAgentTool(
        AgentToolDefinition(
            "react_to_request",
            (
                "Add up to three Unicode or server reactions alongside a short reply or GIF."
            ),
            {
                "type": "object", "properties": {"emoji": {"type": "string"}},
                "required": ["emoji"], "additionalProperties": False,
            },
            usage_note="emoji accepts Unicode, a usable cached server name, :shortcode: or exact custom markup. Optional; include reply text or a GIF in the plan.",
        ),
        react_to_request, action_class=ActionClass.OUTPUT,
        contract=CapabilityContract(source_scope="request_context"),
    ),)
