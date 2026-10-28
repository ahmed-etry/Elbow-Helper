"""Usage logging through public Discord events and the shared error handler."""

import json
import logging

import discord


LOGGER = logging.getLogger("elbow.usage")
_OUTCOME = "elbow_usage_outcome"


def mark_outcome(interaction: discord.Interaction, outcome: str) -> None:
    extras = getattr(interaction, "extras", None)
    if isinstance(extras, dict):
        extras[_OUTCOME] = outcome


def log_command_usage(interaction: discord.Interaction, outcome: str, command=None) -> None:
    if interaction.type != discord.InteractionType.application_command:
        return
    command = command or interaction.command
    name = command.qualified_name if command else (interaction.data or {}).get("name", "unknown")
    # Public timestamps include delivery and dispatch, without racing a start listener.
    elapsed_ms = max(0.0, (discord.utils.utcnow() - interaction.created_at).total_seconds() * 1000)
    LOGGER.info(
        "command name=%s outcome=%s elapsed_ms=%.1f invoker_id=%s",
        json.dumps(name), outcome, elapsed_ms, interaction.user.id,
    )


async def on_app_command_completion(interaction: discord.Interaction, command) -> None:
    log_command_usage(interaction, interaction.extras.get(_OUTCOME, "ok"), command)


async def on_interaction(interaction: discord.Interaction) -> None:
    if interaction.type == discord.InteractionType.component:
        LOGGER.info(
            "component custom_id=%s invoker_id=%s",
            json.dumps((interaction.data or {}).get("custom_id", "unknown")),
            interaction.user.id,
        )
