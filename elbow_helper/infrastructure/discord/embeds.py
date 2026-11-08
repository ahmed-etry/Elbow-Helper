from __future__ import annotations

import discord


def build_status_embed(description: str, color: discord.Color) -> discord.Embed:
    return discord.Embed(description=description, color=color)


def stable_embed_payload(embed: discord.Embed) -> dict:
    """Compare board content without timestamps or Discord's media metadata."""
    payload = embed.to_dict()
    payload.pop("timestamp", None)
    for key in ("thumbnail", "image"):
        media = payload.get(key)
        if isinstance(media, dict):
            for dynamic_key in ("proxy_url", "height", "width"):
                media.pop(dynamic_key, None)
    return payload
