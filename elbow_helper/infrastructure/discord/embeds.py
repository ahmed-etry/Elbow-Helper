from __future__ import annotations

from datetime import datetime, timezone

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


def build_reopened_ticket_embed(
    owner: discord.Member | None, reopened_by: discord.abc.User, *, footer: str,
) -> discord.Embed:
    """The confirmation that replaces a closed ticket's controls once it is reopened."""

    embed = discord.Embed(
        title="Ticket Reopened",
        description=(f"Restored messaging access for {owner.mention}."
                     if owner is not None else "Messaging access restored."),
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Reopened By", value=reopened_by.mention, inline=True)
    embed.set_footer(text=footer)
    return embed
