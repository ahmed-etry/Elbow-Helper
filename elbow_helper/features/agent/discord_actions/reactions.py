"""Request-message reactions without a confirmation preview."""

import re

import discord
import emoji as emoji_library

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..actions.contracts import ActionClass
from ..engine.capability_contract import CapabilityContract
from ..models import RegisteredAgentTool


async def react_to_request(context, arguments):
    message = context.source_message
    if not getattr(message, "id", None) or not callable(getattr(message, "add_reaction", None)):
        return {"error": "There is no message to react to."}
    await require_evidence_access(context)
    value = arguments["emoji"]
    custom = re.fullmatch(r"<a?:\w+:(\d+)>", value)
    if custom:
        value = context.guild.get_emoji(int(custom.group(1)))
        if value is None or not value.is_usable():
            return {"error": "That emoji is unavailable."}
    elif not emoji_library.is_emoji(value):
        return {"error": "That emoji is unavailable."}
    tracker = context.state.reactions
    async with tracker.lock:
        if tracker.count >= 3:
            return {"error": "That emoji is unavailable."}
        try:
            await message.add_reaction(value)
        except discord.HTTPException:
            return {"error": "That emoji is unavailable."}
        tracker.count += 1
        tracker.emojis.append(str(value))
    return {"reacted": True}


def reaction_tools():
    return (RegisteredAgentTool(
        AgentToolDefinition(
            "react_to_request",
            "React to the request message with a Unicode or usable server emoji, "
            "up to three times.",
            {
                "type": "object", "properties": {"emoji": {"type": "string"}},
                "required": ["emoji"], "additionalProperties": False,
            },
        ),
        react_to_request, action_class=ActionClass.OUTPUT,
        contract=CapabilityContract(source_scope="request_context"),
    ),)
