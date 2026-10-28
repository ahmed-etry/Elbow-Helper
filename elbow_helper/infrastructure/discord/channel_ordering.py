from __future__ import annotations

from typing import Sequence

import discord


async def move_text_channel_within_category(
    channel: discord.TextChannel,
    category: discord.CategoryChannel,
    ordered_channels: Sequence[discord.TextChannel],
    target_index: int,
    *,
    reason: str | None = None,
) -> None:
    """Move a text channel using category-scoped ordering only.

    `channel.edit(position=...)` causes discord.py to send a guild-wide bulk
    position update, which can fail if the bot lacks `manage_channels` on any
    unrelated channel elsewhere in the server. `channel.move(...)` scopes the
    payload to sibling channels in the same category/sorting bucket.
    """

    current_index = next(
        (index for index, candidate in enumerate(ordered_channels) if candidate.id == channel.id),
        None,
    )
    if current_index is None:
        raise ValueError("Channel to move is not present in the ordered channel list.")

    safe_target_index = max(0, min(target_index, len(ordered_channels) - 1))
    if current_index == safe_target_index:
        return

    anchor = ordered_channels[safe_target_index]
    if anchor.id == channel.id:
        return

    if safe_target_index < current_index:
        await channel.move(before=anchor, category=category, reason=reason)
    else:
        await channel.move(after=anchor, category=category, reason=reason)
