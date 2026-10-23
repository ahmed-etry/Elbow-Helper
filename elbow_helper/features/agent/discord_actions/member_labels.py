"""Identify message targets by their server display name."""

import re

from ..member_names import member_label
from .safety import resolve_member


async def mentioned_member_labels(guild, text):
    identifiers = dict.fromkeys(int(value) for value in re.findall(r"<@!?([0-9]+)>", text))
    return {identifier: member_label(await resolve_member(guild, identifier), guild=guild)
            for identifier in identifiers}
