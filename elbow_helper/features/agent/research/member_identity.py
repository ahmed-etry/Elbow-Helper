"""Resolve all supplied identity hints before addressing a Discord member."""

import asyncio
from collections import Counter
import unicodedata

import discord

from elbow_helper.configuration.guild import TEST_ACCOUNT_IDS
from ..access import require_evidence_access


def name_parts(value):
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return tuple(''.join(char if char.isalnum() else ' ' for char in normalized).split())


def identity_query(query):
    """Separate explicit Discord mentions from a supplied identity label."""
    if query.isdecimal():
        return {int(query)}, ''
    identifiers, labels = set(), []
    while '<@' in query:
        before, _, tail = query.partition('<@')
        value, close, rest = tail.partition('>')
        numeric = value.removeprefix('!')
        if not close or not numeric.isdecimal():
            labels.extend((before, '<@', tail))
            query = ''
            break
        identifiers.add(int(numeric))
        labels.append(before)
        query = rest
    labels.append(query)
    return identifiers, ' '.join(labels)


def discord_names(member):
    names = [getattr(member, field, None)
             for field in ("display_name", "name", "global_name", "nick")]
    aliases = getattr(member, "aliases", ()) or ()
    names.extend((aliases,) if isinstance(aliases, str) else aliases)
    return tuple(dict.fromkeys(name for name in names if isinstance(name, str) and name))


def matches_discord_identity(member, query):
    """Check a cached identity without a guild chunk or account lookup."""
    identifiers, label = identity_query(query)
    if identifiers and identifiers != {member.id}:
        return False
    parts = name_parts(label)
    words = {word for name in discord_names(member) for word in name_parts(name)}
    return bool(identifiers or parts) and all(
        any(word.startswith(part) for word in words) for part in parts
    )


async def matching_members(context, query):
    """Each name part must match the same member, across their known names."""
    guild = context.guild
    if getattr(guild, "chunked", True) is False:
        await guild.chunk(cache=True)
        if not guild.chunked:
            return []
    members = tuple(member for member in getattr(guild, "members", ())
                    if member.id not in TEST_ACCOUNT_IDS and not getattr(member, 'bot', False))
    identifiers, query = identity_query(query)
    if len(identifiers) > 1:
        return []
    if identifiers:
        members = tuple(member for member in members if member.id in identifiers)
    parts = name_parts(query)
    if identifiers and not parts:
        return list(members)
    if not parts:
        return []
    names = {member.id: discord_names(member) for member in members}
    links = getattr(context, "account_links", None)
    if links is not None and members:
        await require_evidence_access(context)
        accounts = await asyncio.to_thread(links.get_links_for_members, list(names))
        await require_evidence_access(context)
        for identifier, rows in accounts.items():
            names[identifier] += tuple(row["player_name_last_seen"] for row in rows
                                       if row.get("player_name_last_seen"))
    matches = []
    for member in members:
        aliases = tuple(name_parts(name) for name in names[member.id])
        words = {word for alias in aliases for word in alias}
        if all(any(word.startswith(part) for word in words) for part in parts):
            score = 0 if parts in aliases else 1
            matches.append((score, member.display_name.casefold(), member.id, member))
    exact = [item for item in matches if item[0] == 0]
    return [item[-1] for item in sorted(exact if len(exact) == 1 else matches)]


def clarification(query, members, *, limit=8):
    # The query may itself be a mention or an ID; neither belongs in public copy.
    _, query = identity_query(query)
    query = ' '.join(part for part in query.split() if not part.isdecimal()).strip() or 'member'
    query = discord.utils.escape_markdown(query)
    if not members:
        return f"Which member do you mean by {query}? Mention them."
    counts = Counter(member.display_name.casefold() for member in members)
    labels = [discord.utils.escape_markdown(member.display_name)
              + (f" (@{discord.utils.escape_markdown(member.name)})"
                 if counts[member.display_name.casefold()] > 1 else "")
              for member in members[:limit]]
    choices = "; ".join(labels)
    if len(members) > limit:
        choices += "; or someone else (mention them)"
    return f"Which {query}: {choices}?"


