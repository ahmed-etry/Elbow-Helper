"""Deliver the library meme the agent chose for a reply."""

from elbow_helper.infrastructure.ai import AgentToolDefinition
from ..models import AgentAttachment, RegisteredAgentTool, AgentCapabilityEffect
from ..actions.contracts import ActionClass
from ..engine.capability_contract import CapabilityContract
from ..files.gif_media import gif_embed, gif_links
from .gif_library import MEDIA_EXTENSIONS, ITEM_BY_ID, library_gif
from pathlib import Path


GIF_CONTEXT_MESSAGES = 50


def gif_tools():
    return (RegisteredAgentTool(AgentToolDefinition("find_gif",
        "Send a GIF reply when a visual reaction beats text; images are sent without text.",
        {"type":"object", "properties":{"query":{"type":"string", "minLength":1, "maxLength":50,
            "enum": [*ITEM_BY_ID]}},
         "required":["query"], "additionalProperties":False},
        usage_note="Social replies name a library meme in submit_social_reply. Delivered automatically; never paste GIF links."),
        find_gif,
        effect=AgentCapabilityEffect.READ, action_class=ActionClass.OUTPUT,
        contract=CapabilityContract(()), returns="url"),)


def channel_gif_history(messages, bot_id):
    """Keep this bot's meme/image flags from a successful history read, oldest first.

    A sparse or empty snapshot is usable; only an unknown bot identity is unknown.
    """
    def has_gif(message):
        try:
            return bool(gif_links(message) or any(
                embed.type == "image" or gif_embed(embed) for embed in getattr(message, "embeds", ())
            ) or any(
                getattr(item, "content_type", None) == "image/gif"
                or Path(item.filename).suffix.lower() in MEDIA_EXTENSIONS
                for item in getattr(message, "attachments", ())
            ))
        except ValueError:
            return False

    flags = tuple(has_gif(message) for message in messages
                  if bot_id is not None and message.author.id == bot_id)
    if bot_id is None:
        return None
    return flags[-3:]


async def find_gif(context, arguments):
    query = arguments["query"]
    context.state.gif_item_id = None
    if query not in ITEM_BY_ID:
        return {"error": "No fitting GIF in the library."}
    url = library_gif(context, query)
    if isinstance(url, AgentAttachment):
        context.state.gif_attachment = url
        context.state.gif_url = "attachment://" + url.filename
        return {"url": context.state.gif_url}
    if url:
        context.state.gif_attachment = None
        context.state.gif_url = url
    return {"url":url} if url else {"error":"No GIF found."}
