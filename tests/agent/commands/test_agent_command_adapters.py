"""The enabled adapters call feature functions directly."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from elbow_helper.configuration.roles import LEAD_PLUS
from elbow_helper.agent.engine.adapters import run_health_player
from elbow_helper.agent.models import AgentTurnState
from elbow_helper.features.clan_health.commands.player import PlayerHealthCommandResult
from elbow_helper.features.clan_health.export import PreparedHealthExport


class CommandAdapterTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def health_context(workflow):
        member = SimpleNamespace(id=4, roles=[SimpleNamespace(id=next(iter(LEAD_PLUS)))])
        return SimpleNamespace(
            guild=SimpleNamespace(get_member=lambda _: member), member=member,
            bot=SimpleNamespace(get_cog=lambda _: workflow), state=AgentTurnState(),
        )


    async def test_health_uses_the_public_feature_export(self):
        export = PreparedHealthExport(
            "synthetic.xlsx", "Synthetic report", ("Synthetic summary",),
            "https://example.test/sheet", None,
        )
        workflow = SimpleNamespace(run_player_health_export=AsyncMock(
            return_value=PlayerHealthCommandResult("complete", export=export),
        ))
        context = self.health_context(workflow)
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
        context = self.health_context(workflow)
        result = await run_health_player(context, {"account": "#P0"})
        self.assertEqual(result.attachments[0].data, b"synthetic workbook")
        self.assertEqual(result.text, "Synthetic summary")

<<<<<<< 564beefb
=======
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
        workflow._has_player_access = Mock(return_value=True)
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

    async def test_clan_health_uses_the_public_feature_export(self):
        export = PreparedHealthExport(
            "clan.xlsx", "Clan Health", ("Synthetic summary",),
            None, b"synthetic workbook",
        )
        workflow = SimpleNamespace(run_clan_health_export=AsyncMock(
            return_value=ClanHealthCommandResult("complete", export=export),
        ))
        result = await run_health_clan(
            self.health_context(workflow), {"clan": "BEH"},
        )
        self.assertEqual(result.attachments[0].data, b"synthetic workbook")
        workflow.run_clan_health_export.assert_awaited_once_with(
            "BEH", mode="last_30d", date_from=None, date_to=None,
        )

    async def test_clan_health_operation_builds_and_prepares_one_export(self):
        workflow = ClanHealthClanCommandMixin()
        workflow.record = SimpleNamespace(roster=Mock(return_value=(
            None, {"BEH": [{"player_tag": "#PP29L", "player_name": "Synthetic"}]},
        )))
        workflow.analyzer = SimpleNamespace(build_sheets=Mock(return_value=(
            [("Overview", [["Value"]])], [{"status": "Good"}], {},
        )))
        export = PreparedHealthExport(
            "clan.xlsx", "Clan Health", ("Synthetic summary",),
            None, b"synthetic workbook",
        )
        workflow.prepare_health_export = AsyncMock(return_value=export)
        result = await workflow.run_clan_health_export("BEH")
        self.assertEqual(result.status, "complete")
        self.assertIs(result.export, export)
        workflow.prepare_health_export.assert_awaited_once()

    async def test_slash_clan_uses_the_same_public_operation(self):
        workflow = ClanHealthClanCommandMixin()
        workflow._has_access = Mock(return_value=True)
        export = PreparedHealthExport(
            "clan.xlsx", "Clan Health", ("Synthetic summary",),
            None, b"synthetic workbook",
        )
        workflow.run_clan_health_export = AsyncMock(return_value=ClanHealthCommandResult(
            "complete", export=export, timeframe_key="last_30d",
        ))
        workflow.send_prepared_health_export = AsyncMock()
        interaction = SimpleNamespace(user=SimpleNamespace(id=4))
        await workflow._export_clan_health(
            interaction, SimpleNamespace(value="BEH"),
        )
        workflow.run_clan_health_export.assert_awaited_once()
        workflow.send_prepared_health_export.assert_awaited_once_with(interaction, export)
>>>>>>> 917f2109

    async def test_health_custom_period_needs_both_dates(self):
        context = SimpleNamespace(bot=SimpleNamespace(get_cog=Mock()))
        result = await run_health_player(context, {"account": "#P0", "period": "custom"})
        self.assertEqual(result.status, "needs_input")
        context.bot.get_cog.assert_not_called()
