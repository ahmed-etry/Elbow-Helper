"""Application assembly and process entry point."""

from __future__ import annotations

import asyncio

import discord
from discord import app_commands

from elbow_helper.discord.interactions import deny
from elbow_helper.discord.interactions import fail
from elbow_helper.discord.interactions import log_interaction_error
from elbow_helper.discord.interactions import warn
from elbow_helper.configuration.guild import GUILD_ID

from .core.errors import install_asyncio_exception_handler
from .core.lifecycle import ElbowHelperBot
from .core.logging import configure_logging
from .core.paths import ApplicationPaths
from .core.settings import load_runtime_settings
from .infrastructure.clash import ClashClient
from .infrastructure.ai import OpenAITextClient
from .infrastructure.exports import GoogleSheetsPublisher
from .infrastructure.exports import WorkbookWriter


def _format_cooldown_message(retry_after: float) -> str:
    seconds = max(1, round(retry_after))
    if seconds == 1:
        return "That command is on cooldown. Try again in 1 second."
    return f"That command is on cooldown. Try again in {seconds} seconds."


async def handle_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    """Apply the shared user response and diagnostic policy for slash commands."""

    command_name = interaction.command.qualified_name if interaction.command else "unknown"
    root_error = getattr(error, "original", error)

    if isinstance(error, app_commands.CheckFailure) or isinstance(
        root_error,
        app_commands.CheckFailure,
    ):
        await deny(interaction)
        return

    if isinstance(error, app_commands.TransformerError) or isinstance(
        root_error,
        app_commands.TransformerError,
    ):
        await warn(interaction, "That input wasn't recognized. Check the command options and try again.")
        return

    if isinstance(error, app_commands.CommandOnCooldown) or isinstance(
        root_error,
        app_commands.CommandOnCooldown,
    ):
        cooldown_error = (
            error if isinstance(error, app_commands.CommandOnCooldown) else root_error
        )
        await warn(interaction, _format_cooldown_message(cooldown_error.retry_after))
        return

    log_interaction_error(interaction, root_error, source=f"app_command.{command_name}")
    await fail(interaction)


def create_bot(
    paths: ApplicationPaths,
    clash_client: ClashClient,
    text_generator: OpenAITextClient,
    google_publisher: GoogleSheetsPublisher,
    workbook_writer: WorkbookWriter,
) -> ElbowHelperBot:
    """Construct the Discord bot without loading settings or starting network I/O."""

    bot = ElbowHelperBot(
        paths=paths,
        guild_id=GUILD_ID,
        clash_client=clash_client,
        text_generator=text_generator,
        google_publisher=google_publisher,
        workbook_writer=workbook_writer,
    )
    bot.tree.error(handle_app_command_error)
    return bot


async def main() -> None:
    """Load process configuration, assemble the application, and run the bot."""

    paths = ApplicationPaths.discover()
    settings = load_runtime_settings(paths)
    configure_logging(paths)
    install_asyncio_exception_handler(asyncio.get_running_loop())

    google_publisher = GoogleSheetsPublisher(
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret,
        refresh_token=settings.google_oauth_refresh_token,
        folder_id=settings.google_drive_folder_id,
    )
    workbook_writer = WorkbookWriter()
    text_generator = OpenAITextClient(settings.openai_api_key)
    async with ClashClient(settings.coc_api_key) as clash_client:
        bot = create_bot(
            paths,
            clash_client,
            text_generator,
            google_publisher,
            workbook_writer,
        )
        async with bot:
            await bot.start(settings.require_discord_token())


def run() -> None:
    """Run the asynchronous application from a synchronous process entry point."""

    asyncio.run(main())
