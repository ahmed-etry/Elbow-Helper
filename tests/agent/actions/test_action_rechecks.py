"""Preview checks distinguish expected changes from unexpected failures."""

from dataclasses import replace
import unittest
from unittest.mock import AsyncMock


from elbow_helper.agent.actions.contracts import ChangePreview


class ActionRecheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_recheck_failures_are_logged_and_block_confirmation(self):
        for error in (RuntimeError("Synthetic"), KeyError("Synthetic"), TypeError("Synthetic")):
            with self.subTest(error=type(error).__name__):
                check = AsyncMock(side_effect=error)
                preview = ChangePreview(("Synthetic",), check)
                with self.assertLogs("elbow_helper.agent.actions.contracts", level="ERROR") as logs:
                    self.assertFalse(await preview.recheck())
                self.assertTrue(any("Traceback" in line for line in logs.output))
                check.assert_awaited_once()
                self.assertIs(replace(preview, summary="Synthetic").recheck, preview.recheck)
