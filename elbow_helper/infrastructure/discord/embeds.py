from __future__ import annotations

import discord


def build_status_embed(description: str, color: discord.Color) -> discord.Embed:
    return discord.Embed(description=description, color=color)
