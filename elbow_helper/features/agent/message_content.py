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
