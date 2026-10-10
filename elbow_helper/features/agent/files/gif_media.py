"""Recognize GIF messages and resolve provider animations or Discord's still fallback."""

import asyncio
from html import unescape
from html.parser import HTMLParser
import re
import time
from urllib.parse import urlsplit

import discord

PROVIDERS = ("klipy.com", "giphy.com", "tenor.com")
URLS = re.compile(r"https://[^\s<>\"'\\]+")
MAX_MEDIA_BYTES = 8 * 1024 * 1024


def provider_url(url):
    if not isinstance(url, str):
        return False
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    return parsed.scheme == "https" and any(host == item or host.endswith("." + item)
                                            for item in PROVIDERS)


def gif_links(message):
    return [url.rstrip(").,>") for url in URLS.findall(getattr(message, "content", "") or "")
            if provider_url(url)]


def gif_embed(embed):
    return embed.type == "gifv" or provider_url(getattr(embed, "url", None))


def has_image_content(message):
    from .images import EXTENSIONS, MIME_TYPES
    from pathlib import PurePosixPath
    return bool(gif_links(message) or any(
        getattr(item, "content_type", None) in MIME_TYPES
        or PurePosixPath(item.filename).suffix.lower() in EXTENSIONS
        for item in getattr(message, "attachments", ())
    ) or any(embed.type in ("image", "gifv") or gif_embed(embed)
             for embed in getattr(message, "embeds", ())))


class MediaTags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta" and values.get("property") in ("og:image", "og:video", "og:video:url"):
            self.urls.append(values.get("content", ""))


async def download(context, url):
    if not isinstance(url, str):
        return None
    host = urlsplit(url).hostname or ""
    discord_media = host in {"cdn.discordapp.com", "media.discordapp.net"}
    if not provider_url(url) and not (discord_media and urlsplit(url).scheme == "https"):
        return None
    try:
        data = await context.bot.http.get_from_cdn(url)
    except (discord.HTTPException, OSError, TimeoutError):
        return None
    return data if isinstance(data, bytes) and 0 < len(data) <= MAX_MEDIA_BYTES else None


async def with_embeds(context, source, wait_until):
    if not gif_links(source) or any(gif_embed(embed) for embed in getattr(source, "embeds", ())):
        return source
    fetch = getattr(getattr(source, "channel", None), "fetch_message", None)
    if not callable(fetch):
        return source
    while time.monotonic() < wait_until:
        try:
            async with asyncio.timeout(max(0, wait_until - time.monotonic())):
                await asyncio.sleep(min(0.25, max(0, wait_until - time.monotonic())))
                refreshed = await fetch(source.id)
            if any(gif_embed(embed) for embed in getattr(refreshed, "embeds", ())):
                return refreshed
        except (discord.HTTPException, OSError, TimeoutError):
            break
    return source


async def gif_media(context, source, wait_until):
    source = await with_embeds(context, source, wait_until)
    links = gif_links(source)
    embeds = [embed for embed in getattr(source, "embeds", ()) if gif_embed(embed)]
    if not links and not embeds:
        return ()
    groups, used = [], set()
    for link in dict.fromkeys(links):
        matching = [embed for embed in embeds if getattr(embed, "url", None) == link
                    or len(links) == 1]
        used.update(id(embed) for embed in matching)
        groups.append(([link], matching))
    groups.extend(([], [embed]) for embed in embeds if id(embed) not in used)
    result = []
    for selected_links, selected_embeds in groups[:4]:
        data = await resolve_media(context, selected_links, selected_embeds)
        if data is not None:
            result.append(data)
    return tuple(result)


async def resolve_media(context, links, embeds):
    direct, stills = [], []
    for embed in embeds:
        for name in ("video", "image", "thumbnail"):
            part = getattr(embed, name, None)
            for field in ("url", "proxy_url"):
                url = getattr(part, field, None)
                if not url:
                    continue
                if urlsplit(url).path.lower().endswith(".gif"):
                    direct.append(url)
                elif name != "video":
                    stills.append(url)
    for url in dict.fromkeys([*direct, *links]):
        data = await download(context, url)
        if not data:
            continue
        if data.startswith((b"GIF87a", b"GIF89a")):
            return data
        if url in direct:
            continue
        page = unescape(data.decode("utf-8", errors="replace").replace("\\/", "/"))
        tags = MediaTags()
        tags.feed(page)
        candidates = [*tags.urls, *URLS.findall(page)]
        candidates = [candidate for candidate in dict.fromkeys(candidates)
                      if provider_url(candidate) and urlsplit(candidate).path.endswith(".gif")]
        for candidate in candidates[:6]:
            animation = await download(context, candidate)
            if animation and animation.startswith((b"GIF87a", b"GIF89a")):
                return animation
    for url in dict.fromkeys(stills):
        data = await download(context, url)
        if data:
            return data
    return None
