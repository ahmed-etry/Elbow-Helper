"""Render known Discord member and role IDs as names in generated reply prose."""

import re

import discord

from .grounding import records


_PROTECTED = re.compile(r"(https?://[^\s<>]+|<[@#][!&]?\d+>)")
_RAW_ID = re.compile(r"(?<![\w/#])`?(\d{17,20})`?(?!\w)")


def named_reply(answer, context):
    """Keep mentions and source URLs intact; resolve only typed, known identities."""
    names = {}
    for row in records(context.state.grounding_evidence):
        for identity, labels in (("member_id", ("display_name", "member_name")),
                                 ("role_id", ("role_name",))):
            value = row.get(identity)
            name = next((row[key] for key in labels if isinstance(row.get(key), str) and row[key]), None)
            if value is not None and name:
                names[str(value)] = name
    for identity in _RAW_ID.findall(answer):
        for getter, field in (("get_member", "display_name"), ("get_role", "name")):
            lookup = getattr(context.guild, getter, None)
            entity = lookup(int(identity)) if callable(lookup) else None
            if entity is not None and str(getattr(entity, "id", "")) == identity:
                name = getattr(entity, field, None)
                if isinstance(name, str) and name:
                    names[identity] = name

    def prose(text):
        for identity, name in names.items():
            label = discord.utils.escape_mentions(discord.utils.escape_markdown(name))
            text = re.sub(r"(" + re.escape(label) + r"\*{0,2})\s*\(\s*`?"
                          + re.escape(identity) + r"`?\s*\)", r"\1", text)
        return _RAW_ID.sub(lambda match: discord.utils.escape_mentions(
            discord.utils.escape_markdown(names[match[1]])) if match[1] in names else match[0], text)

    return "".join(part if index % 2 else prose(part)
                   for index, part in enumerate(_PROTECTED.split(answer)))
