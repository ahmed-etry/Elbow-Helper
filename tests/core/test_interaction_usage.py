"""Synthetic usage events require no private Discord dispatch hooks or network."""

from datetime import datetime, timedelta, timezone
import io
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

import discord
from discord import app_commands

from elbow_helper.app import create_bot, handle_app_command_error
from elbow_helper.core.logging import configure_logging
from elbow_helper.core.paths import ApplicationPaths
from elbow_helper.discord.interactions import deny, fail
from elbow_helper.discord.usage import on_app_command_completion, on_interaction


class InteractionUsageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 27, tzinfo=timezone.utc)
        self.command = SimpleNamespace(name="run", qualified_name="usage run")
        self.interaction = SimpleNamespace(
            type=discord.InteractionType.application_command,
            command=self.command,
            created_at=self.now - timedelta(milliseconds=25),
            user=SimpleNamespace(id=42, name="private-name"),
            message=SimpleNamespace(content="private-message"),
            data={"name": "usage", "options": [{"name": "note", "value": "private-argument"}]},
            extras={},
        )

    def assert_command_log(self, logs, outcome, name="usage run"):
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].levelno, logging.INFO)
        self.assertEqual(logs.records[0].getMessage(),
                         f'command name="{name}" outcome={outcome} elapsed_ms=25.0 invoker_id=42')
        self.assertIsNone(logs.records[0].exc_info)

    def test_create_bot_registers_public_listeners_and_error_handler(self):
        bot = SimpleNamespace(add_listener=Mock(), tree=SimpleNamespace(error=Mock()))
        with patch("elbow_helper.app.ElbowHelperBot", return_value=bot):
            self.assertIs(create_bot(None, None, None, None, None), bot)
        self.assertEqual(bot.add_listener.call_args_list,
                         [call(on_app_command_completion), call(on_interaction)])
        bot.tree.error.assert_called_once_with(handle_app_command_error)

    async def test_command_logs_only_on_completion_without_arguments(self):
        with self.assertNoLogs("elbow.usage", level="INFO"):
            await on_interaction(self.interaction)
        with (patch("elbow_helper.discord.usage.discord.utils.utcnow", return_value=self.now),
              self.assertLogs("elbow.usage", level="INFO") as logs):
            await on_app_command_completion(self.interaction, self.command)
        self.assert_command_log(logs, "ok")

    async def test_tree_errors_log_failed_or_denied_once(self):
        for error, outcome in (
            (app_commands.AppCommandError("private-error"), "failed"),
            (app_commands.CheckFailure("private-check"), "denied"),
            (app_commands.CommandInvokeError(self.command, app_commands.CheckFailure("private-check")), "denied"),
        ):
            with self.subTest(outcome=outcome):
                with (patch("elbow_helper.discord.usage.discord.utils.utcnow", return_value=self.now),
                      patch("elbow_helper.app.deny", new=AsyncMock()) as denial,
                      patch("elbow_helper.app.fail", new=AsyncMock()) as failure,
                      patch("elbow_helper.app.log_interaction_error"),
                      self.assertLogs("elbow.usage", level="INFO") as logs):
                    await handle_app_command_error(self.interaction, error)
                self.assert_command_log(logs, outcome)
                self.assertEqual(denial.await_count, int(outcome == "denied"))
                self.assertEqual(failure.await_count, int(outcome == "failed"))

    async def test_feedback_outcomes_survive_normal_command_return(self):
        for feedback, outcome in ((deny, "denied"), (fail, "failed")):
            with self.subTest(outcome=outcome):
                with patch("elbow_helper.discord.interactions._send_feedback", new=AsyncMock()):
                    await feedback(self.interaction)
                with (patch("elbow_helper.discord.usage.discord.utils.utcnow", return_value=self.now),
                      self.assertLogs("elbow.usage", level="INFO") as logs):
                    await on_app_command_completion(self.interaction, self.command)
                self.assert_command_log(logs, outcome)

    async def test_error_without_resolved_command_uses_name_only(self):
        self.interaction.command = None
        with (patch("elbow_helper.discord.usage.discord.utils.utcnow", return_value=self.now),
              patch("elbow_helper.app.fail", new=AsyncMock()),
              patch("elbow_helper.app.log_interaction_error"),
              self.assertLogs("elbow.usage", level="INFO") as logs):
            await handle_app_command_error(self.interaction, app_commands.AppCommandError("private-error"))
        self.assert_command_log(logs, "failed", "usage")

    async def test_component_press_logs_id_without_outcome_text_or_values(self):
        self.interaction.type = discord.InteractionType.component
        self.interaction.data = {"custom_id": "usage:select", "values": ["private-value"]}
        self.interaction.extras = {"elbow_usage_outcome": "failed"}
        with self.assertLogs("elbow.usage", level="INFO") as logs:
            await on_interaction(self.interaction)
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(logs.records[0].levelno, logging.INFO)
        self.assertEqual(logs.records[0].getMessage(),
                         'component custom_id="usage:select" invoker_id=42')

    async def test_autocomplete_and_modal_text_are_not_logged(self):
        for kind in (discord.InteractionType.autocomplete, discord.InteractionType.modal_submit):
            with self.subTest(kind=kind):
                self.interaction.type = kind
                self.interaction.data = {"custom_id": "usage:modal", "value": "private-text"}
                with self.assertNoLogs("elbow.usage", level="INFO"):
                    await on_interaction(self.interaction)
                    await on_app_command_completion(self.interaction, self.command)


class UsageLoggingPolicyTests(unittest.TestCase):
    def test_usage_reaches_file_but_not_console(self):
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        console = io.StringIO()
        try:
            with TemporaryDirectory() as directory, patch("sys.stderr", console):
                paths = ApplicationPaths.from_project_root(Path(directory))
                configure_logging(paths)
                logging.getLogger("elbow.usage").info(
                    'component custom_id="usage:button" invoker_id=42')
                for handler in root.handlers:
                    handler.flush()
                self.assertIn('component custom_id="usage:button" invoker_id=42',
                              (paths.log_directory / "bot.log").read_text(encoding="utf-8"))
                self.assertEqual(console.getvalue(), "")
                for handler in root.handlers:
                    handler.close()
        finally:
            root.handlers[:] = handlers
            root.setLevel(level)
