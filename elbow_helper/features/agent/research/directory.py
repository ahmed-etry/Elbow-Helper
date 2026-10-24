"""Live channel locators checked against the existing destination boundary."""

import json

import discord

from ..access import can_access_message_channel


async def scoped_channel_directory(context):
    """Supply discovery metadata, never messages or access inferred from room descriptions."""
    channels = {}
    for channel in (context.source_message.channel,
                    *getattr(context.guild, "channels", ()),
                    *getattr(context.guild, "threads", ())):
        if isinstance(channel, (discord.CategoryChannel, discord.VoiceChannel, discord.StageChannel)):
            continue
        # Discover private/archived threads explicitly; do not fetch memberships
        # or expand the available source set while assembling a prompt.
        if isinstance(channel, discord.Thread) and channel.is_private():
            continue
        if channel.id in channels or not await can_access_message_channel(context, channel, relevant=False):
            continue
        channels[channel.id] = getattr(channel, "name", str(channel.id))
    return json.dumps(channels, ensure_ascii=False, separators=(",", ":"))
