"""Select the current channel message that the agent's own answer will reply to."""

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import require_evidence_access
from ..actions.contracts import ActionClass
from ..engine.capability_contract import CapabilityContract
from ..models import RegisteredAgentTool
from .messages import _reply_target
from .safety import DiscordActionRefused


async def set_reply_target(context, arguments):
    await require_evidence_access(context)
    channel = getattr(context, "delivery_channel", None) or context.source_message.channel
    try:
        await _reply_target(context, channel, arguments["message_id"])
    except DiscordActionRefused as error:
        return {"error": str(error)}
    await require_evidence_access(context)
    context.state.source_channels.add(channel.id)
    context.state.reply_to = arguments["message_id"]
    context.state.ping_author = arguments.get("ping_author", False)
    return {"reply_to": context.state.reply_to, "ping_author": context.state.ping_author}


async def answer_reply_target(context, channel):
    if context is None or getattr(context.state, "reply_to", None) is None:
        return None
    current = getattr(context, "delivery_channel", None) or context.source_message.channel
    if channel.id != current.id:
        return None
    await require_evidence_access(context)
    return await _reply_target(context, channel, context.state.reply_to)


def reply_target_tools():
    return (RegisteredAgentTool(
        AgentToolDefinition(
            "set_discord_reply_target",
            "Select the current-channel message your answer replies to.",
            {"type": "object", "properties": {
                "message_id": {"type": "integer", "minimum": 1},
                "ping_author": {"type": "boolean"},
            }, "required": ["message_id"], "additionalProperties": False},
            usage_note="No Confirm; answer normally. Ping only with ping_author.",
        ),
        set_reply_target, action_class=ActionClass.OUTPUT,
        contract=CapabilityContract(
            entity_fields=(("message_id", "discord_message"),),
            filter_fields=("ping_author",), source_scope="request_context",
        ),
    ),)
