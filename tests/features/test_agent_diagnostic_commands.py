"""Clash connection checks share one feature operation."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.commands.adapters.diagnostics import run_api
from elbow_helper.features.diagnostics.cog import ClashConnectionCheck, DebugCog


class DiagnosticCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_uses_feature_check_and_its_visible_fields(self):
        response = SimpleNamespace(
            error=None, status=200, latency_ms=42, headers={},
            payload_object={"state": "inWar", "opponent": {"name": "Synthetic"}},
        )
        client = SimpleNamespace(configured=True, get=AsyncMock(return_value=response))
        workflow = DebugCog(SimpleNamespace(), client)
        context = SimpleNamespace(bot=SimpleNamespace(get_cog=lambda _: workflow))
        outcome = await run_api(context, {})
        self.assertIn("Clash API Status", outcome.text)
        self.assertIn("Opponent: Synthetic", outcome.text)
        client.get.assert_awaited_once()

    async def test_slash_uses_the_same_check(self):
        workflow = DebugCog(SimpleNamespace(), SimpleNamespace(configured=True))
        embed = discord.Embed(title="Synthetic status")
        workflow.check_clash_connection = AsyncMock(return_value=ClashConnectionCheck(
            "complete", embed=embed,
        ))
        interaction = SimpleNamespace(
            user=SimpleNamespace(roles=[SimpleNamespace(id=next(iter(CORE)))]),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await DebugCog.coc_api_test.callback(workflow, interaction)
        workflow.check_clash_connection.assert_awaited_once()
        interaction.followup.send.assert_awaited_once_with(
            embed=embed, ephemeral=False,
        )
