"""Request image acquisition through authorized Discord sources."""

import asyncio
import json
import re
import time
from pathlib import PurePosixPath

import discord

from elbow_helper.infrastructure.ai.agent import AgentImage
from elbow_helper.infrastructure.gifs.images import animation_sheet
from PIL import Image, UnidentifiedImageError
from ..access import accessible_message_channel, can_access_message_channel

from .gif_media import gif_embed, gif_media

MIME_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
EXTENSIONS = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_REQUEST_IMAGES = 4
_MESSAGE_LINK = re.compile(
    r"https?://(?:(?:canary|ptb)\.)?(?:discord\.com|discordapp\.com)/channels/"
    r"(\d+)/(\d+)/(\d+)(?=$|[/?#\s)>])"
)


def _source_status(context, text):
    state = getattr(context, "state", None)
    if state is not None:
        state.image_source_status.append(text)


async def _sources(context):
    """Current sources first, then previously seen sources from the newest turn."""
    seen = set()
    replies = []
    for source in getattr(context, "attachment_sources", ()):
        channel = getattr(source, "channel", None)
        key = (getattr(channel, "id", None), source.id)
        if hasattr(context, "guild") and source is not context.source_message:
            if channel is not None:
                replies.append(key)
            continue
        if key in seen:
            continue
        seen.add(key)
        if hasattr(context, "guild") and (
            channel is None or not await can_access_message_channel(context, channel)
        ):
            continue
        label = "the asker's message" if source is context.source_message else "the replied-to message"
        yield source, label

    state = getattr(context, "state", None)
    request = getattr(state, "request_text", "") or getattr(context.source_message, "content", "")
    linked = [(int(channel), int(message)) for guild, channel, message in _MESSAGE_LINK.findall(request)
              if hasattr(context, "guild") and int(guild) == context.guild.id]
    retained = [source for turn in reversed(getattr(context, "history", ())) if turn.record is not None
                for source in getattr(turn.record, "image_sources", ())]
    for key, label in [*((source, "the replied-to message") for source in replies),
                       *((source, "a linked message") for source in linked),
                       *((source, "an earlier conversation image") for source in retained)]:
        if key in seen:
            continue
        seen.add(key)
        channel_id, message_id = key
        channel = await accessible_message_channel(context, channel_id)
        if channel is None:
            _source_status(context, f"Image source cannot be read (message {message_id}).")
            continue
        try:
            source = await channel.fetch_message(message_id)
        except discord.NotFound:
            _source_status(context, f"Image source is gone (message {message_id}).")
            continue
        except (discord.HTTPException, OSError):
            _source_status(context, f"Image source is unavailable right now (message {message_id}).")
            continue
        if not await can_access_message_channel(context, channel):
            _source_status(context, f"Image source cannot be read (message {message_id}).")
            continue
        yield source, label


def _remember_source(context, source):
    state = getattr(context, "state", None)
    channel_id = getattr(getattr(source, "channel", None), "id", None)
    if state is not None and channel_id is not None:
        key = (channel_id, source.id)
        if key not in state.image_sources:
            state.image_sources.append(key)
        state.source_channels.add(channel_id)


async def request_images(context):
    result = []
    deadline = getattr(context, "deadline_monotonic", None)
    available = 10 if deadline is None else max(0, min(10, deadline - time.monotonic() - 2))
    if available <= 0:
        return ()
    try:
        async with asyncio.timeout(available):
            return await _request_images(context, result, time.monotonic() + min(2, available))
    except TimeoutError:
        return tuple(result)


async def _request_images(context, result, wait_until):
    async for source, label_source in _sources(context):
        label_source = _source_label(context, source, label_source)
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
                image = await model_image(data, mime, label_source, source.id, len(result) + 1)
                if image is not None:
                    result.append(image)
                    _remember_source(context, source)
            if len(result) == MAX_REQUEST_IMAGES:
                return tuple(result)
        for sticker in getattr(source, "stickers", ()):
            label = f"{label_source}; sticker {json.dumps(sticker.name, ensure_ascii=False)}"
            mime = {
                discord.StickerFormatType.png: "image/png",
                discord.StickerFormatType.apng: "image/png",
                discord.StickerFormatType.gif: "image/gif",
            }.get(sticker.format)
            image = None
            if mime:
                try:
                    data = await sticker.read()
                except (discord.HTTPException, OSError, asyncio.TimeoutError):
                    data = None
                if isinstance(data, bytes) and 0 < len(data) <= MAX_IMAGE_BYTES:
                    image = await model_image(
                        data, mime, label, source.id, len(result) + 1,
                        animated=sticker.format == discord.StickerFormatType.apng,
                    )
            _remember_source(context, source)
            if image is None:
                _source_status(context, f"Sticker image could not be viewed: {label} (message {source.id}).")
            else:
                result.append(image)
            if len(result) == MAX_REQUEST_IMAGES:
                return tuple(result)
        for data in await gif_media(context, source, wait_until):
            image = await model_image(data, "image/jpeg", label_source, source.id, len(result) + 1,
                                      gif_source=True)
            if image is not None:
                result.append(image)
                _remember_source(context, source)
            if len(result) == MAX_REQUEST_IMAGES:
                return tuple(result)
        for embed in getattr(source, "embeds", ()):
            if gif_embed(embed):
                continue
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
                    image = await model_image(data, mime, label_source, source.id, len(result) + 1)
                    if image is not None:
                        result.append(image)
                        _remember_source(context, source)
                if len(result) == MAX_REQUEST_IMAGES:
                    return tuple(result)
                break
    return tuple(result)


def _source_label(context, source, label):
    """Keep visual speakers and reply targets attached to their own media."""
    bot_id = getattr(getattr(getattr(context, "bot", None), "user", None), "id", None)

    def author_identity(author):
        identifier = getattr(author, "id", None)
        return {"name": getattr(author, "display_name", getattr(author, "name", "")),
                "member_id": identifier, "is_you": identifier is not None and identifier == bot_id}

    details = {}
    if getattr(source, "author", None) is not None:
        details["sender"] = author_identity(source.author)
    if getattr(source, "content", ""):
        details["message_text"] = source.content
    reference = getattr(source, "reference", None)
    reply_id = getattr(reference, "message_id", None)
    if reply_id is not None:
        details["replies_to_message_id"] = reply_id
        channel_id = getattr(getattr(source, "channel", None), "id", None)
        targets = (*getattr(context, "attachment_sources", ()), getattr(reference, "resolved", None))
        for target in targets:
            if (channel_id is not None and getattr(target, "id", None) == reply_id
                    and getattr(getattr(target, "channel", None), "id", None) == channel_id
                    and getattr(target, "author", None) is not None):
                details["reply_recipient"] = author_identity(target.author)
                break
    return label + ("; " + json.dumps(details, ensure_ascii=False) if details else "")


async def model_image(data, mime, label_source, message_id, number, *, gif_source=False, animated=False):
    mime = _image_mime(data, mime)
    label = f"Image {number}, from {label_source} (message {message_id})"
    if gif_source:
        label = f"Still of one GIF {number}, from {label_source} (message {message_id})"
    if mime == "image/gif" or animated:
        try:
            data = await asyncio.to_thread(animation_sheet, data)
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
            return None
        mime = "image/png"
        kind = "sticker" if animated else "GIF"
        label = f"Frames of one {kind} {number}, from {label_source} (message {message_id})"
    return AgentImage(mime, data, label)


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
