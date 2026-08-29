"""Discord bot construction and startup lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time

import discord
from discord.ext import commands

from elbow_helper.infrastructure.clash import ClashClient
from elbow_helper.infrastructure.ai import OpenAITextClient
from elbow_helper.infrastructure.exports import GoogleSheetsPublisher
from elbow_helper.infrastructure.exports import LocalExportStore
from elbow_helper.infrastructure.exports import WorkbookWriter

from .logging import log_box
from .paths import ApplicationPaths


LOGGER = logging.getLogger("elbow.boot")

REQUIRED_EXTENSIONS = (
    "elbow_helper.features.support_tickets",
    "elbow_helper.features.event_stats",
    "elbow_helper.features.help",
    "elbow_helper.features.wars",
    "elbow_helper.features.achievements",
    "elbow_helper.features.hibernation",
    "elbow_helper.features.member_lifecycle",
    "elbow_helper.features.diagnostics",
    "elbow_helper.features.leadership_news",
    "elbow_helper.features.clan_transfers",
    "elbow_helper.features.message_automation",
    "elbow_helper.features.role_connections",
    "elbow_helper.features.account_links",
    "elbow_helper.features.recruitment",
    "elbow_helper.features.clan_health",
    "elbow_helper.features.attack_plans",
    "elbow_helper.features.records",
    "elbow_helper.features.rosters",
    "elbow_helper.features.cwl",
    "elbow_helper.features.clan_reporting",
    "elbow_helper.features.examination",
    "elbow_helper.discord.command_registry",
)
OPTIONAL_EXTENSIONS: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtensionFailure:
    """An extension that could not be loaded during startup."""

    name: str
    error: commands.ExtensionError


@dataclass(frozen=True, slots=True)
class ExtensionLoadReport:
    """Result of loading the explicitly classified startup extensions."""

    loaded: tuple[str, ...]
    required_failures: tuple[ExtensionFailure, ...]
    optional_failures: tuple[ExtensionFailure, ...]
    elapsed_seconds: float

    @property
    def degraded(self) -> bool:
        return bool(self.optional_failures)


class RequiredExtensionLoadError(RuntimeError):
    """Raised when the bot cannot load every required extension."""

    def __init__(self, report: ExtensionLoadReport):
        self.report = report
        names = ", ".join(failure.name for failure in report.required_failures)
        super().__init__(f"Required extensions failed to load: {names}")


async def load_extensions(
    bot: commands.Bot,
    *,
    required: tuple[str, ...] = REQUIRED_EXTENSIONS,
    optional: tuple[str, ...] = OPTIONAL_EXTENSIONS,
) -> ExtensionLoadReport:
    """Load classified extensions and reject an incomplete required startup."""

    started_at = time.perf_counter()
    loaded: list[str] = []
    required_failures: list[ExtensionFailure] = []
    optional_failures: list[ExtensionFailure] = []

    for extension in required:
        try:
            await bot.load_extension(extension)
            loaded.append(extension)
        except commands.ExtensionError as error:
            required_failures.append(ExtensionFailure(extension, error))
            LOGGER.exception("Required extension failed: %s", extension)

    for extension in optional:
        try:
            await bot.load_extension(extension)
            loaded.append(extension)
        except commands.ExtensionError as error:
            optional_failures.append(ExtensionFailure(extension, error))
            LOGGER.exception("Optional extension failed: %s", extension)

    report = ExtensionLoadReport(
        loaded=tuple(loaded),
        required_failures=tuple(required_failures),
        optional_failures=tuple(optional_failures),
        elapsed_seconds=time.perf_counter() - started_at,
    )
    if report.required_failures:
        raise RequiredExtensionLoadError(report)
    return report


def build_intents() -> discord.Intents:
    """Build the Discord gateway intents required by the current features."""

    intents = discord.Intents.default()
    intents.messages = True
    intents.message_content = True
    intents.guilds = True
    intents.members = True
    intents.reactions = True
    return intents


async def update_bot_avatar(bot: commands.Bot, paths: ApplicationPaths) -> None:
    """Upload the configured avatar only when its local file has changed."""

    try:
        if not paths.avatar_file.exists():
            LOGGER.warning("Avatar file not found at %s; skipping avatar update", paths.avatar_file)
            return
        if bot.user is None:
            LOGGER.warning("Bot user is unavailable; skipping avatar update")
            return

        current_mtime = paths.avatar_file.stat().st_mtime
        if paths.avatar_state_file.exists():
            try:
                last_mtime = float(paths.avatar_state_file.read_text(encoding="utf-8").strip())
            except ValueError:
                last_mtime = 0.0
            if current_mtime <= last_mtime:
                return

        await bot.user.edit(avatar=paths.avatar_file.read_bytes())
        paths.avatar_directory.mkdir(parents=True, exist_ok=True)
        paths.avatar_state_file.write_text(str(current_mtime), encoding="utf-8")
    except (OSError, ValueError, discord.HTTPException) as error:
        LOGGER.exception("Error setting bot avatar: %s", error)


class ElbowHelperBot(commands.Bot):
    """Discord bot with explicit ownership of its startup lifecycle."""

    def __init__(
        self,
        *,
        paths: ApplicationPaths,
        guild_id: int,
        clash_client: ClashClient,
        text_generator: OpenAITextClient,
        google_publisher: GoogleSheetsPublisher,
        workbook_writer: WorkbookWriter,
        required_extensions: tuple[str, ...] = REQUIRED_EXTENSIONS,
        optional_extensions: tuple[str, ...] = OPTIONAL_EXTENSIONS,
    ):
        super().__init__(command_prefix="!", intents=build_intents())
        self.paths = paths
        self.guild_id = guild_id
        self.clash_client = clash_client
        self.text_generator = text_generator
        self.google_publisher = google_publisher
        self.workbook_writer = workbook_writer
        self.local_exports = LocalExportStore(paths.data_root / ".exports")
        self.required_extensions = required_extensions
        self.optional_extensions = optional_extensions
        self.boot_complete = asyncio.Event()
        self.extension_report: ExtensionLoadReport | None = None
        self._boot_started_at = 0.0

    async def setup_hook(self) -> None:
        self._boot_started_at = time.perf_counter()
        self.extension_report = await load_extensions(
            self,
            required=self.required_extensions,
            optional=self.optional_extensions,
        )

    async def on_ready(self) -> None:
        if self.boot_complete.is_set():
            return

        report = self.extension_report
        if report is None:
            raise RuntimeError("Discord connected before extension startup completed.")

        boot_lines = [
            (
                "Extensions loaded: "
                f"{len(report.loaded)} ok, {len(report.optional_failures)} optional failed "
                f"({report.elapsed_seconds:.2f}s)"
            ),
            f"Connected as: {self.user}",
        ]

        await update_bot_avatar(self, self.paths)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="/help",
            )
        )

        synced_count = 0
        try:
            guild = discord.Object(id=self.guild_id)
            synced = await self.tree.sync(guild=guild)
            synced_count = len(synced)
        except discord.HTTPException as error:
            LOGGER.exception("Slash command sync failed: %s", error)

        boot_elapsed_seconds = time.perf_counter() - self._boot_started_at
        boot_lines.append(f"Slash commands synced: {synced_count}")
        boot_lines.append(f"Ready. Boot time: {boot_elapsed_seconds:.2f}s")
        log_box(LOGGER, boot_lines)
        self.boot_complete.set()
