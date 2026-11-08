from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Sequence

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


def _names(items: Sequence[str], limit: int = 5) -> str:
    text = ", ".join(items[:limit])
    return f"{text} (+{len(items) - limit} more)" if len(items) > limit else text


async def reorder_ticket_channels(
    category: discord.CategoryChannel,
    bot_user: discord.abc.User | None,
    *,
    is_ticket: Callable[[discord.TextChannel], bool],
    sort_key: Callable[[discord.TextChannel], Any],
    reason: str,
    logger: logging.Logger,
    warn: Callable[..., None],
    clear: Callable[..., None],
    pause_seconds: float = 0.0,
    skip_forbidden: bool = False,
) -> bool:
    """Move a category's ticket channels into `sort_key` order, one move at a time.

    Returns False when the bot could not be resolved or some channel needed
    `manage_channels` it does not have. Raises the last Discord error a move
    hit. With `skip_forbidden`, a forbidden channel is left alone and the rest
    still move; otherwise the first forbidden move ends the pass.
    """

    guild = category.guild
    me = guild.me
    if me is None and bot_user:
        me = guild.get_member(bot_user.id)
    if me is None and bot_user:
        try:
            me = await guild.fetch_member(bot_user.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
            warn("member_resolve", "Skipping ticket reorder: unable to resolve bot member object: %s", error)
            return False
    if me is None:
        warn("member_missing", "Skipping ticket reorder: bot member object unavailable.")
        return False
    clear("member_resolve", "member_missing")

    tracked_ids = {channel.id for channel in category.text_channels if is_ticket(channel)}
    if not tracked_ids:
        return True
    blocked_ids: set[int] = set()
    no_permission: set[str] = set()
    forbidden: list[str] = []
    failed: list[str] = []
    error: discord.HTTPException | None = None
    attempts = moved = 0
    for _ in range(max(1, len(tracked_ids) * 2)):
        # Moving one channel shifts the others, so plan again from live positions each pass.
        live = [ch for ch in category.text_channels if ch.id in tracked_ids and is_ticket(ch)]
        if len(live) != len(tracked_ids):
            logger.debug("%s: ticket set changed while reordering; stopping.", reason)
            break
        current = sorted(live, key=lambda ch: ch.position)
        desired = sorted(live, key=sort_key)
        target_index = None
        for index, (have, want) in enumerate(zip(current, desired)):
            if have.id == want.id or want.id in blocked_ids:
                continue
            permissions = want.permissions_for(me)
            if permissions.manage_channels and permissions.view_channel:
                target_index = index
                break
            blocked_ids.add(want.id)
            no_permission.add(f"{want.name} ({want.id})")
        if target_index is None:
            break
        channel = desired[target_index]
        position = current[target_index].position
        if channel.position == position:
            continue

        attempts += 1
        try:
            await move_text_channel_within_category(
                channel, category, current, target_index, reason=reason,
            )
            moved += 1
        except discord.Forbidden as caught:
            error = caught
            forbidden.append(f"{channel.name} ({channel.id})")
            logger.debug(
                "%s: forbidden moving %s from position %s to %s (status=%s code=%s): %s",
                reason, channel.name, channel.position, position,
                getattr(caught, "status", "unknown"), getattr(caught, "code", "unknown"),
                (getattr(caught, "text", "") or str(caught)).replace("\n", " ").strip(),
            )
            if not skip_forbidden:
                break
            blocked_ids.add(channel.id)
        except discord.HTTPException as caught:
            error = caught
            failed.append(f"{channel.name} ({channel.id}): {caught}")
            break
        finally:
            if pause_seconds:
                await asyncio.sleep(pause_seconds)

    if no_permission or forbidden or failed:
        logger.debug(
            "%s: moved %s of %s attempted. Without permission: %s. Forbidden: %s. Failed: %s",
            reason, moved, attempts,
            _names(sorted(no_permission)), _names(forbidden), _names(failed, 3),
        )
    if error is not None:
        raise error
    return not no_permission
