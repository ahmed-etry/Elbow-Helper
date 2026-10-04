"""Text evidence shared by local context and retrieved Discord messages."""

import re

import discord

from .identity import member_identity
from .models import AgentIdentity


def message_text(message: discord.Message) -> str:
    parts = [str(message.content or "").strip()]
    for embed in getattr(message, "embeds", ()):
        parts.extend(str(value) for value in (embed.title, embed.description) if value)
        for field in embed.fields:
            parts.append(f"{field.name}: {field.value}")
    if message.attachments:
        names = ", ".join(item.filename for item in message.attachments)
        parts.append(f"[attachments, contents not read: {names}]")
    return "\n".join(part for part in parts if part)


def render_member_mentions(
    content: str, message: discord.Message, *, identity: AgentIdentity | None = None,
) -> str:
    mentioned = {member.id: member for member in getattr(message, "mentions", ())}
    get_member = getattr(getattr(message, "guild", None), "get_member", None)

    def render(match: re.Match[str]) -> str:
        member_id = int(match.group(1))
        if identity is not None and member_id == identity.member_id:
            return f"@{identity.display_name} (you, member_id={member_id})"
        member = member_identity(mentioned.get(member_id))
        if member is None and callable(get_member):
            member = member_identity(get_member(member_id))
        if member is None or member.member_id != member_id:
            return match.group(0)
        return f"@{member.display_name} (member_id={member_id})"

    return re.sub(r"<@!?(\d+)>", render, content)


DISCORD_MESSAGE_LIMIT = 2_000


def chunk_response(content: str) -> list[str]:
    remaining = str(content or "").strip()
    chunks: list[str] = []
    while remaining:
        if len(remaining) <= DISCORD_MESSAGE_LIMIT:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, DISCORD_MESSAGE_LIMIT)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, DISCORD_MESSAGE_LIMIT)
        if split_at <= 0:
            split_at = DISCORD_MESSAGE_LIMIT
        chunk = remaining[:split_at].rstrip()
        chunks.append(chunk or remaining[:DISCORD_MESSAGE_LIMIT])
        remaining = remaining[split_at:].lstrip()
    return chunks
