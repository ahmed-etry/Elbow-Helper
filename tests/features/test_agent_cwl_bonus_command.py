"""The CWL bonus command keeps report creation in its feature service."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.commands.adapters.cwl_bonus import run_cwl_bonus
from elbow_helper.features.cwl.bonus.service import BonusReportError


class CwlBonusCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_delivers_feature_workbook_and_discards_staging_file(self):
        with TemporaryDirectory() as directory:
            workbook = Path(directory) / "report.xlsx"
            workbook.write_bytes(b"synthetic workbook")
            report = SimpleNamespace(
                scope_label="BEH", season="2026-09", workbook_path=workbook,
                workbook_name="report.xlsx", google_link=None, google_warning=None,
                eligible_count=2, ineligible_count=1, attack_count=5,
                warnings=(),
            )
            service = SimpleNamespace(
                create=AsyncMock(return_value=report), discard=AsyncMock(),
            )
            workflow = SimpleNamespace(bonus_reports=service)
            context = SimpleNamespace(bot=SimpleNamespace(get_cog=lambda _: workflow))
            outcome = await run_cwl_bonus(context, {"clan": "BEH"})
        self.assertEqual(outcome.attachments[0].data, b"synthetic workbook")
        self.assertIn("Eligible players: 2", outcome.text)
        service.create.assert_awaited_once_with("BEH", None)
        service.discard.assert_awaited_once_with(report)

    async def test_report_error_uses_feature_message(self):
        service = SimpleNamespace(create=AsyncMock(side_effect=BonusReportError("no_seasons")))
        workflow = SimpleNamespace(
            bonus_reports=service,
            bonus_report_error_message=lambda _: "No completed season.",
        )
        context = SimpleNamespace(bot=SimpleNamespace(get_cog=lambda _: workflow))
        outcome = await run_cwl_bonus(context, {"clan": "BEH"})
        self.assertEqual(outcome.text, "No completed season.")
