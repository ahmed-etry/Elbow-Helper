from __future__ import annotations

import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from unittest.mock import patch

import discord
from discord.ext import commands

from elbow_helper.app import create_bot
from elbow_helper.core.lifecycle import ElbowHelperBot
from elbow_helper.core.lifecycle import RequiredExtensionLoadError
from elbow_helper.core.lifecycle import load_extensions
from elbow_helper.core.lifecycle import sync_guild_commands
from elbow_helper.core.logging import CompactTransientDuplicateFilter
from elbow_helper.core.logging import TransientExternalFailurePolicy
from elbow_helper.core.logging import UnifiedLogFormatter
from elbow_helper.core.paths import ApplicationPaths
from elbow_helper.infrastructure.clash import ClashClient
from elbow_helper.infrastructure.ai import OpenAITextClient
from elbow_helper.infrastructure.exports import GoogleSheetsPublisher
from elbow_helper.infrastructure.exports import WorkbookWriter


class _ExtensionLoader:
    def __init__(self, failures: set[str] | None = None):
        self.failures = failures or set()
        self.loaded: list[str] = []

    async def load_extension(self, name: str) -> None:
        if name in self.failures:
            raise commands.ExtensionNotFound(name)
        self.loaded.append(name)


class ExtensionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_required_extension_failure_rejects_startup(self) -> None:
        bot = _ExtensionLoader({"elbow_helper.features.required"})

        with self.assertLogs("elbow.boot", level=logging.ERROR):
            with self.assertRaises(RequiredExtensionLoadError) as raised:
                await load_extensions(
                    bot,  # type: ignore[arg-type]
                    required=("elbow_helper.features.working", "elbow_helper.features.required"),
                )

        report = raised.exception.report
        self.assertEqual(report.loaded, ("elbow_helper.features.working",))
        self.assertEqual(
            tuple(failure.name for failure in report.required_failures),
            ("elbow_helper.features.required",),
        )
        self.assertFalse(report.degraded)

    async def test_optional_extension_failure_marks_startup_degraded(self) -> None:
        bot = _ExtensionLoader({"elbow_helper.features.optional"})

        with self.assertLogs("elbow.boot", level=logging.ERROR):
            report = await load_extensions(
                bot,  # type: ignore[arg-type]
                required=("elbow_helper.features.required",),
                optional=("elbow_helper.features.optional",),
            )

        self.assertEqual(report.loaded, ("elbow_helper.features.required",))
        self.assertEqual(
            tuple(failure.name for failure in report.optional_failures),
            ("elbow_helper.features.optional",),
        )
        self.assertTrue(report.degraded)

    async def test_command_sync_retries_before_startup_can_complete(self) -> None:
        response = SimpleNamespace(
            status=503,
            reason="Unavailable",
            headers={},
        )
        error = discord.HTTPException(response, "sync unavailable")
        tree = SimpleNamespace(
            sync=AsyncMock(side_effect=[error, error, [SimpleNamespace(name="help")]])
        )

        with (
            patch(
                "elbow_helper.core.lifecycle.asyncio.sleep",
                new=AsyncMock(),
            ) as sleep,
            self.assertLogs("elbow.boot", level=logging.WARNING),
        ):
            synced = await sync_guild_commands(tree, 123)

        self.assertEqual(len(synced), 1)
        self.assertEqual(tree.sync.await_count, 3)
        self.assertEqual(
            [call.args[0] for call in sleep.await_args_list],
            [1.0, 2.0],
        )

    async def test_command_sync_failure_rejects_startup(self) -> None:
        response = SimpleNamespace(
            status=503,
            reason="Unavailable",
            headers={},
        )
        error = discord.HTTPException(response, "sync unavailable")
        tree = SimpleNamespace(sync=AsyncMock(side_effect=error))

        with (
            patch(
                "elbow_helper.core.lifecycle.asyncio.sleep",
                new=AsyncMock(),
            ),
            self.assertLogs("elbow.boot", level=logging.ERROR),
            self.assertRaises(discord.HTTPException),
        ):
            await sync_guild_commands(tree, 123)

        self.assertEqual(tree.sync.await_count, 3)

    async def test_setup_rejects_command_sync_failure(self) -> None:
        bot = SimpleNamespace(
            required_extensions=(),
            optional_extensions=(),
            extension_report=None,
            synced_command_count=None,
            tree=object(),
            guild_id=123,
        )
        sync_error = RuntimeError("sync failed")

        with (
            patch(
                "elbow_helper.core.lifecycle.load_extensions",
                new=AsyncMock(return_value=SimpleNamespace()),
            ),
            patch(
                "elbow_helper.core.lifecycle.sync_guild_commands",
                new=AsyncMock(side_effect=sync_error),
            ),
            self.assertRaisesRegex(RuntimeError, "sync failed"),
        ):
            await ElbowHelperBot.setup_hook(bot)

        self.assertIsNone(bot.synced_command_count)


class ApplicationAssemblyTests(unittest.TestCase):
    def test_create_bot_builds_without_loading_extensions(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            paths = ApplicationPaths.from_project_root(Path(temporary_directory))

            clash_client = ClashClient(None)
            bot = create_bot(
                paths,
                clash_client,
                OpenAITextClient(None),
                GoogleSheetsPublisher(
                    client_id=None,
                    client_secret=None,
                    refresh_token=None,
                    folder_id=None,
                ),
                WorkbookWriter(),
            )

        self.assertIsInstance(bot, ElbowHelperBot)
        self.assertEqual(bot.paths, paths)
        self.assertIs(bot.clash_client, clash_client)
        self.assertEqual(
            bot.local_exports.directory,
            paths.data_root / ".exports",
        )
        self.assertEqual(bot.extensions, {})
        self.assertTrue(bot.intents.message_content)
        self.assertTrue(bot.intents.members)


class LoggingPolicyTests(unittest.TestCase):
    @staticmethod
    def _warning_record(message: str) -> logging.LogRecord:
        return logging.LogRecord(
            name="elbow_helper.features.example",
            level=logging.WARNING,
            pathname=__file__,
            lineno=100,
            msg=message,
            args=(),
            exc_info=None,
        )

    def test_timeout_is_classified_as_transient(self) -> None:
        self.assertTrue(
            TransientExternalFailurePolicy.is_transient_exception(
                TimeoutError("request timed out")
            )
        )
        self.assertFalse(
            TransientExternalFailurePolicy.is_transient_exception(
                ValueError("invalid stored value")
            )
        )

    def test_duplicate_transient_records_are_throttled(self) -> None:
        duplicate_filter = CompactTransientDuplicateFilter(cooldown_seconds=300.0)

        self.assertTrue(duplicate_filter.filter(self._warning_record("connection timed out")))
        self.assertFalse(duplicate_filter.filter(self._warning_record("connection timed out")))

    def test_formatter_preserves_the_existing_logger_tags(self) -> None:
        formatter = UnifiedLogFormatter("%(log_tag)s %(message)s")

        rendered = formatter.format(self._warning_record("Example"))

        self.assertEqual(rendered, "[EXAMPLE] Example")
