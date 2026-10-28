"""Server display names, with usernames only for nickname collisions."""

from collections import Counter

import discord


def _name_key(member):
    return " ".join(member.display_name.split()).casefold()


def duplicate_names(members):
    counts = Counter(_name_key(member) for member in members
                     if isinstance(getattr(member, "display_name", None), str))
    return {name for name, count in counts.items() if count > 1}


def identity_fields(member, duplicates):
    return {
        "display_name": member.display_name,
        **({"username": member.name} if _name_key(member) in duplicates else {}),
    }


def member_label(member, *, guild=None):
    guild = guild if guild is not None else getattr(member, "guild", None)
    fields = identity_fields(member, duplicate_names(getattr(guild, "members", ())))
    label = discord.utils.escape_markdown(" ".join(fields["display_name"].split()))
    if "username" in fields:
        label += f" (@{discord.utils.escape_markdown(fields['username'])})"
    return label
