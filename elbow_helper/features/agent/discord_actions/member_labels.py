"""Identify message targets with Discord's display name and unique username."""

import re

import discord

from .safety import resolve_member


def member_label(member):
    display_name = discord.utils.escape_markdown(" ".join(member.display_name.split()))
    username = discord.utils.escape_markdown(member.name)
    return f"{display_name} (@{username})"


async def mentioned_member_labels(guild, text):
    identifiers = dict.fromkeys(int(value) for value in re.findall(r"<@!?([0-9]+)>", text))
    return {identifier: member_label(await resolve_member(guild, identifier))
            for identifier in identifiers}
