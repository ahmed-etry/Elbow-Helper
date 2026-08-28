from __future__ import annotations

import logging
from typing import Any

import discord


_MISSING = discord.utils.MISSING
LOGGER = logging.getLogger("elbow.interactions")
DEFAULT_FAILURE_MESSAGE = "I couldn't finish that right now. Try again in a moment."
UNKNOWN_INTERACTION_CODE = 10062


def _bind_view_message(view: discord.ui.View, message: discord.Message | None) -> None:
    bind_message = getattr(view, "bind_message", None)
    if callable(bind_message):
        bind_message(message)


async def send_bound_view(
    interaction: discord.Interaction,
    *,
    view: discord.ui.View,
    content: str | None = None,
    embed: discord.Embed | None = None,
    ephemeral: bool = False,
) -> discord.ui.View:
    await interaction.response.send_message(
        content=content,
        embed=embed,
        view=view,
        ephemeral=ephemeral,
    )
    _bind_view_message(view, await interaction.original_response())
    return view


async def edit_bound_view(
    interaction: discord.Interaction,
    *,
    view: discord.ui.View,
    content: object = _MISSING,
    embed: object = _MISSING,
) -> discord.ui.View:
    kwargs: dict[str, Any] = {"view": view}
    if content is not _MISSING:
        kwargs["content"] = content
    if embed is not _MISSING:
        kwargs["embed"] = embed
    await interaction.response.edit_message(**kwargs)
    try:
        _bind_view_message(view, await interaction.original_response())
    except (discord.NotFound, discord.HTTPException):
        _bind_view_message(view, interaction.message if interaction.message else None)
    return view


async def edit_original_bound_view(
    interaction: discord.Interaction,
    *,
    view: discord.ui.View,
    content: object = _MISSING,
    embed: object = _MISSING,
) -> discord.ui.View:
    kwargs: dict[str, Any] = {"view": view}
    if content is not _MISSING:
        kwargs["content"] = content
    if embed is not _MISSING:
        kwargs["embed"] = embed
    await interaction.edit_original_response(**kwargs)
    _bind_view_message(view, await interaction.original_response())
    return view


async def _send_feedback(
    interaction: discord.Interaction,
    message: str,
    *,
    public: bool = False,
) -> None:
    ephemeral = not public
    if interaction.response.is_done():
        try:
            await interaction.followup.send(message, ephemeral=ephemeral)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LOGGER.debug("Failed to send followup feedback", exc_info=True)
        return

    try:
        await interaction.response.send_message(message, ephemeral=ephemeral)
        return
    except discord.InteractionResponded:
        pass
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        LOGGER.debug("Failed to send interaction response feedback", exc_info=True)
        return

    try:
        await interaction.followup.send(message, ephemeral=ephemeral)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        LOGGER.debug("Failed to recover with followup feedback", exc_info=True)


async def send_ephemeral(interaction: discord.Interaction, message: str) -> None:
    await _send_feedback(interaction, message)


async def fail(
    interaction: discord.Interaction,
    message: str = DEFAULT_FAILURE_MESSAGE,
    *,
    public: bool = False,
) -> None:
    await _send_feedback(interaction, message, public=public)


async def warn(
    interaction: discord.Interaction,
    message: str,
    *,
    public: bool = False,
) -> None:
    await _send_feedback(interaction, message, public=public)


async def succeed(
    interaction: discord.Interaction,
    message: str,
    *,
    public: bool = False,
) -> None:
    await _send_feedback(interaction, message, public=public)


async def progress(
    interaction: discord.Interaction,
    message: str,
    *,
    public: bool = False,
) -> None:
    await _send_feedback(interaction, message, public=public)


def log_interaction_error(
    interaction: discord.Interaction,
    error: Exception,
    *,
    source: str,
) -> None:
    user_id = getattr(interaction.user, "id", None)
    if is_unknown_interaction_error(error):
        LOGGER.debug(
            "%s expired before it could be acknowledged | guild=%s channel=%s user=%s",
            source,
            interaction.guild_id,
            interaction.channel_id,
            user_id,
        )
        return
    LOGGER.exception(
        "%s failed | guild=%s channel=%s user=%s",
        source,
        interaction.guild_id,
        interaction.channel_id,
        user_id,
        exc_info=error,
    )


def is_unknown_interaction_error(error: Exception) -> bool:
    return isinstance(error, discord.NotFound) and getattr(error, "code", None) == UNKNOWN_INTERACTION_CODE


async def deny(
    interaction: discord.Interaction,
    *,
    action: str = "use this command",
    public: bool = False,
) -> None:
    if action == "use this command":
        message = "You don't have permission to use this command."
    else:
        message = f"You don't have permission to {action}."
    await _send_feedback(interaction, message, public=public)
