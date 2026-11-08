"""The CWL bonus command keeps report creation in its feature service."""

from pathlib import Path
from tests.temp_directory import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.agent.features.cwl.bonus_export import run_cwl_bonus


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
