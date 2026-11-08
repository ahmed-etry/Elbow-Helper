"""The enabled adapters call feature functions directly."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from elbow_helper.configuration.roles import LEAD_PLUS
from elbow_helper.agent.engine.adapters import run_health_player
from elbow_helper.agent.engine.models import AgentTurnState
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


    async def test_health_custom_period_needs_both_dates(self):
        context = SimpleNamespace(bot=SimpleNamespace(get_cog=Mock()))
        result = await run_health_player(context, {"account": "#P0", "period": "custom"})
        self.assertEqual(result.status, "needs_input")
        context.bot.get_cog.assert_not_called()
