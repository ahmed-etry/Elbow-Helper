"""The enabled adapters call feature functions directly."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from elbow_helper.features.agent.commands.adapters import run_health_player, run_opinion


class CommandAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_result_keeps_feature_parts_out_of_public_text(self):
        ticket = object()
        workflow = SimpleNamespace(
            resolve_opinion_ticket=Mock(return_value=ticket),
            build_ticket_second_opinion=AsyncMock(return_value=["Synthetic private result"]),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow), guild=object(), member=object(),
        )
        result = await run_opinion(context, {"ticket": "101"})
        self.assertEqual(result.visibility, "private")
        self.assertEqual(result.private_parts, ("Synthetic private result",))
        self.assertEqual(result.text, "")
        workflow.build_ticket_second_opinion.assert_awaited_once_with(ticket)

    async def test_missing_or_unresolved_input_does_not_call_feature(self):
        workflow = SimpleNamespace(
            resolve_opinion_ticket=Mock(return_value=None),
            build_ticket_second_opinion=AsyncMock(),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow), guild=object(), member=object(),
        )
        for values in ({}, {"ticket": "unknown"}):
            result = await run_opinion(context, values)
            self.assertEqual(result.status, "needs_input")
        workflow.build_ticket_second_opinion.assert_not_awaited()

    async def test_health_uses_feature_report_and_publisher(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.xlsx"
            path.write_bytes(b"synthetic workbook")
            report = SimpleNamespace(
                sheets=[("Overview", [["Value"], ["7"]])],
                workbook_title="Synthetic report", workbook_name="synthetic.xlsx",
                summary_lines=["Synthetic summary"],
            )
            workflow = SimpleNamespace(
                build_player_health_export=AsyncMock(return_value=report),
                write_health_workbook=AsyncMock(return_value=path),
                google_publisher=SimpleNamespace(
                    upload_workbook=AsyncMock(return_value=("https://example.test/sheet", None)),
                ),
                local_exports=SimpleNamespace(delete=Mock()),
            )
            context = SimpleNamespace(bot=SimpleNamespace(get_cog=lambda _: workflow))
            result = await run_health_player(context, {"account": "#P0"})
            self.assertEqual(result.status, "complete")
            self.assertIn("https://example.test/sheet", result.text)
            self.assertEqual(result.attachments, ())
            workflow.build_player_health_export.assert_awaited_once()
            workflow.write_health_workbook.assert_awaited_once_with(report.sheets)
            workflow.local_exports.delete.assert_called_once_with(path)

    async def test_health_fallback_carries_complete_workbook_bytes(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.xlsx"
            path.write_bytes(b"synthetic workbook")
            report = SimpleNamespace(
                sheets=[], workbook_title="Synthetic report",
                workbook_name="synthetic.xlsx", summary_lines=["Synthetic summary"],
            )
            workflow = SimpleNamespace(
                build_player_health_export=AsyncMock(return_value=report),
                write_health_workbook=AsyncMock(return_value=path),
                google_publisher=SimpleNamespace(upload_workbook=AsyncMock(return_value=(None, None))),
                local_exports=SimpleNamespace(delete=Mock()),
            )
            context = SimpleNamespace(bot=SimpleNamespace(get_cog=lambda _: workflow))
            result = await run_health_player(context, {"account": "#P0"})
            self.assertEqual(result.attachments[0].data, b"synthetic workbook")
            self.assertEqual(result.text, "Synthetic summary")

    async def test_health_custom_period_needs_both_dates(self):
        context = SimpleNamespace(bot=SimpleNamespace(get_cog=Mock()))
        result = await run_health_player(context, {"account": "#P0", "period": "custom"})
        self.assertEqual(result.status, "needs_input")
        context.bot.get_cog.assert_not_called()
