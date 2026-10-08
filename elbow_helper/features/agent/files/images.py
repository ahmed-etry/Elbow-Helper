"""Request image acquisition through authorized Discord sources."""

import asyncio
from pathlib import PurePosixPath

import discord

from elbow_helper.infrastructure.ai.agent import AgentImage

MIME_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
EXTENSIONS = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


async def request_images(context):
    result = []
    for source in context.attachment_sources:
        label_source = (
            "the asker's message" if source is context.source_message else "the replied-to message"
        )
        for attachment in getattr(source, "attachments", ()):
            mime = str(getattr(attachment, "content_type", "") or "").split(";")[0].lower()
            if not mime:
                mime = EXTENSIONS.get(PurePosixPath(attachment.filename).suffix.lower())
            if (
                mime not in MIME_TYPES
                or getattr(attachment, "size", MAX_IMAGE_BYTES+1) > MAX_IMAGE_BYTES
            ):
                continue
            try:
                data = await attachment.read()
            except (discord.HTTPException, OSError, asyncio.TimeoutError):
                continue
            if isinstance(data, bytes) and 0 < len(data) <= MAX_IMAGE_BYTES:
                result.append(AgentImage(
                    _image_mime(data, mime), data,
                    f"Image {len(result)+1}, from {label_source} (message {source.id})",
                ))
            if len(result) == 4:
                return tuple(result)
        for embed in getattr(source, "embeds", ()):
            if embed.type not in ("image", "gifv", "rich"):
                continue
            for part in (embed.image, embed.thumbnail):
                url = getattr(part, "proxy_url", None)
                if not url:
                    continue
                try:
                    data = await context.bot.http.get_from_cdn(url)
                except (discord.HTTPException, OSError, asyncio.TimeoutError):
                    continue
                mime = EXTENSIONS.get(
                    PurePosixPath(url.split("?", 1)[0]).suffix.lower(), "image/jpeg",
                )
                if isinstance(data, bytes) and 0 < len(data) <= MAX_IMAGE_BYTES:
                    result.append(AgentImage(
                        _image_mime(data, mime), data,
                        f"Image {len(result)+1}, from {label_source} (message {source.id})",
                    ))
                if len(result) == 4:
                    return tuple(result)
                break
    return tuple(result)


def _image_mime(data, fallback):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return fallback
