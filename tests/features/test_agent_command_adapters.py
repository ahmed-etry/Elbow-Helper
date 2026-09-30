"""The enabled adapters call feature functions directly."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.configuration.roles import LEAD_PLUS
from elbow_helper.features.agent.commands.adapters import run_health_player, run_opinion
from elbow_helper.features.agent.commands.adapters.clan_health import run_health_settings
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.clan_health.commands.player import (
    ClanHealthPlayerCommandMixin, PlayerHealthCommandResult,
)
from elbow_helper.features.clan_health.export import PreparedHealthExport


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

    async def test_health_uses_the_public_feature_export(self):
        export = PreparedHealthExport(
            "synthetic.xlsx", "Synthetic report", ("Synthetic summary",),
            "https://example.test/sheet", None,
        )
        workflow = SimpleNamespace(run_player_health_export=AsyncMock(
            return_value=PlayerHealthCommandResult("complete", export=export),
        ))
        context = SimpleNamespace(bot=SimpleNamespace(get_cog=lambda _: workflow))
        result = await run_health_player(context, {"account": "#P0"})
        self.assertEqual(result.status, "complete")
        self.assertIn("https://example.test/sheet", result.text)
        self.assertEqual(result.attachments, ())
        workflow.run_player_health_export.assert_awaited_once_with(
            "#P0", mode="last_30d", date_from=None, date_to=None,
        )

    async def test_health_fallback_carries_complete_workbook_bytes(self):
        export = PreparedHealthExport(
            "synthetic.xlsx", "Synthetic report", ("Synthetic summary",),
            None, b"synthetic workbook",
        )
        workflow = SimpleNamespace(run_player_health_export=AsyncMock(
            return_value=PlayerHealthCommandResult("complete", export=export),
        ))
        context = SimpleNamespace(bot=SimpleNamespace(get_cog=lambda _: workflow))
        result = await run_health_player(context, {"account": "#P0"})
        self.assertEqual(result.attachments[0].data, b"synthetic workbook")
        self.assertEqual(result.text, "Synthetic summary")

    async def test_player_health_operation_builds_and_prepares_one_export(self):
        workflow = ClanHealthPlayerCommandMixin()
        report = SimpleNamespace(
            workbook_name="synthetic.xlsx", workbook_title="Synthetic report",
            summary_lines=["Synthetic summary"], sheets=[("Overview", [["Value"]])],
        )
        export = PreparedHealthExport(
            "synthetic.xlsx", "Synthetic report", ("Synthetic summary",),
            None, b"synthetic workbook",
        )
        workflow.build_player_health_export = AsyncMock(return_value=report)
        workflow.prepare_health_export = AsyncMock(return_value=export)
        result = await workflow.run_player_health_export("#P0")
        self.assertIs(result.export, export)
        self.assertEqual(result.status, "complete")
        workflow.build_player_health_export.assert_awaited_once()
        workflow.prepare_health_export.assert_awaited_once_with(
            workbook_name=report.workbook_name,
            workbook_title=report.workbook_title,
            summary_lines=report.summary_lines, sheets=report.sheets,
        )

    async def test_slash_player_uses_the_same_public_operation(self):
        workflow = ClanHealthPlayerCommandMixin()
        export = PreparedHealthExport(
            "synthetic.xlsx", "Synthetic report", ("Synthetic summary",),
            None, b"synthetic workbook",
        )
        workflow._has_access = Mock(return_value=True)
        workflow.run_player_health_export = AsyncMock(return_value=PlayerHealthCommandResult(
            "complete", export=export, season_key="last 30d",
        ))
        workflow.send_prepared_health_export = AsyncMock()
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=4), response=SimpleNamespace(defer=AsyncMock()),
        )
        await workflow._export_player_health(interaction, "#P0")
        workflow.run_player_health_export.assert_awaited_once()
        workflow.send_prepared_health_export.assert_awaited_once_with(interaction, export)

    async def test_health_custom_period_needs_both_dates(self):
        context = SimpleNamespace(bot=SimpleNamespace(get_cog=Mock()))
        result = await run_health_player(context, {"account": "#P0", "period": "custom"})
        self.assertEqual(result.status, "needs_input")
        context.bot.get_cog.assert_not_called()

    async def test_health_settings_opens_the_feature_management_screen(self):
        member = SimpleNamespace(id=4, roles=[SimpleNamespace(id=next(iter(LEAD_PLUS)))])
        context = SimpleNamespace(
            guild=SimpleNamespace(get_member=lambda _: member), member=member,
            bot=SimpleNamespace(get_cog=lambda _: object()), state=AgentTurnState(),
        )
        result = await run_health_settings(context, {"clan": "BEH"})
        self.assertIsNotNone(result.private_panel)
        interaction = SimpleNamespace(user=member)
        with patch("elbow_helper.features.agent.commands.adapters.clan_health.ClanConfigHomeView.open",
                   new_callable=AsyncMock) as open_panel:
            await result.private_panel(interaction)
        open_panel.assert_awaited_once_with(interaction, "BEH")
