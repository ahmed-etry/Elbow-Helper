"""Text evidence shared by local context and retrieved Discord messages."""

import discord


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
