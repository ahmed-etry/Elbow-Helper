"""Preview checks distinguish expected changes from unexpected failures."""

from dataclasses import replace
import unittest
from unittest.mock import AsyncMock

import discord

from elbow_helper.features.agent.actions.contracts import ActionRefused, ChangePreview


class ActionRecheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_recheck_failures_are_logged_and_block_confirmation(self):
        for error in (RuntimeError("Synthetic"), KeyError("Synthetic"), TypeError("Synthetic")):
            with self.subTest(error=type(error).__name__):
                check = AsyncMock(side_effect=error)
                preview = ChangePreview(("Synthetic",), check)
                with self.assertLogs("elbow_helper.features.agent.actions.contracts", level="ERROR") as logs:
                    self.assertFalse(await preview.recheck())
                self.assertTrue(any("Traceback" in line for line in logs.output))
                check.assert_awaited_once()
                self.assertIs(replace(preview, summary="Synthetic").recheck, preview.recheck)

    async def test_expected_refusals_do_not_log_a_traceback(self):
        for error in (ValueError("Synthetic"), ActionRefused("Synthetic"), discord.DiscordException()):
            with self.subTest(error=type(error).__name__):
                preview = ChangePreview(("Synthetic",), AsyncMock(side_effect=error))
                with self.assertNoLogs("elbow_helper.features.agent.actions.contracts", level="ERROR"):
                    self.assertFalse(await preview.recheck())
