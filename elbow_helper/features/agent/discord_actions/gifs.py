"""Find a GIF page link for the agent reply."""

from elbow_helper.infrastructure.ai import AgentToolDefinition
from ..models import RegisteredAgentTool, AgentCapabilityEffect
from ..actions.contracts import ActionClass
from ..engine.capability_contract import CapabilityContract
from .gif_choice import choose_gif


def gif_tools(client):
    if client is None or not client.configured:
        return ()
    return (RegisteredAgentTool(AgentToolDefinition("find_gif",
        "Choose one GIF for a separate reply message; never paste GIF links.",
        {"type":"object", "properties":{"query":{"type":"string", "minLength":1, "maxLength":50}},
         "required":["query"], "additionalProperties":False}), find_gif,
        effect=AgentCapabilityEffect.READ, action_class=ActionClass.OUTPUT,
        contract=CapabilityContract(()), returns="url"),)


async def find_gif(context, arguments):
    url = await choose_gif(context, arguments["query"])
    if url:
        context.state.gif_url = url
    return {"url":url} if url else {"error":"No GIF found."}
