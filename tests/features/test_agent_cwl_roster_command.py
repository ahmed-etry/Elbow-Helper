"""The CWL roster command shares analysis and exports a private workbook."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

from elbow_helper.features.agent.capabilities.cwl.roster_export import run_cwl_roster
from elbow_helper.features.cwl.roster.commands import CwlRosterMixin
from elbow_helper.features.cwl.roster.export import CwlRosterExportMixin


class _Workflow(CwlRosterMixin, CwlRosterExportMixin):
    pass


class CwlRosterCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_feature_preparation_and_agent_attachment(self):
        workflow = _Workflow()
        workflow._analyze_roster_history = MagicMock(return_value={
            "wars": [object()], "season_metrics": {}, "mega_metrics": {},
            "seasons": (), "profiles": {}, "latest_leagues": {},
        })
        workflow._build_roster_candidates = AsyncMock(return_value={
            "signed_account_count": 2, "signed_member_count": 1,
            "candidates": (), "signed_tags": (), "records": (),
            "links_by_user": {},
        })
        workflow._build_roster_workbook = MagicMock(return_value=("sheet",))
        workflow.build_cwl_roster_attachment = AsyncMock(
            return_value=("roster.xlsx", b"workbook"),
        )
        guild = SimpleNamespace(id=1)
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow), guild=guild,
        )
        outcome = await run_cwl_roster(context, {"history": 6})
        self.assertEqual(outcome.visibility, "private")
        self.assertIn("latest 6 available seasons per clan", outcome.text)
        self.assertEqual(outcome.attachments[0].filename, "roster.xlsx")
        self.assertEqual(outcome.attachments[0].data, b"workbook")
        workflow._analyze_roster_history.assert_called_once_with(6)
        workflow._build_roster_workbook.assert_called_once()
        workflow.build_cwl_roster_attachment.assert_awaited_once_with(("sheet",))

    async def test_empty_history_skips_export(self):
        workflow = _Workflow()
        workflow._analyze_roster_history = MagicMock(return_value={"wars": []})
        workflow.build_cwl_roster_attachment = AsyncMock()
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=SimpleNamespace(id=1),
        )
        outcome = await run_cwl_roster(context, {})
        self.assertIn("No completed CWL seasons", outcome.text)
        workflow.build_cwl_roster_attachment.assert_not_awaited()
