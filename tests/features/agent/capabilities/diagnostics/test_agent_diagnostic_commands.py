"""Clash connection checks share one feature operation."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.capabilities.diagnostics.commands import diagnostic_adapters, run_api
from elbow_helper.features.agent.commands.bridge import build_command_tools
from elbow_helper.features.agent.datasets.clash import read_clash
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.plan.format import capability_list
from elbow_helper.features.diagnostics.cog import ClashConnectionCheck, DebugCog


class DiagnosticCommandTests(unittest.IsolatedAsyncioTestCase):
    def test_model_catalogue_limits_status_checks_and_names_scoped_evidence_tools(self):
        bot = SimpleNamespace(tree=SimpleNamespace(get_commands=lambda guild=None: (
            [DebugCog.coc_api_test] if guild is not None else []
        )))
        tools, _ = build_command_tools(bot, diagnostic_adapters())
        registry = {**build_agent_tools(), **tools}
        line = next(line for line in capability_list(registry).splitlines()
                    if line.startswith("check_clash_api: "))
        self.assertIn("General Clash API status only.", line)
        self.assertIn("Check read or collector failures", line)
        for name, scope in (("query_bot_data", "stored evidence"),
                            ("read_clash", "same kind and tags")):
            self.assertIn(name, registry)
            self.assertIn(f"{name} ({scope})", line)
        self.assertIn("answer from evidence.", line)
        self.assertIn("clan:str=", line)

    async def test_specific_read_reports_its_failure_despite_general_check_success(self):
        workflow = SimpleNamespace(check_clash_connection=AsyncMock(
            return_value=ClashConnectionCheck(
                "complete", embed=discord.Embed(title="Synthetic status"),
            ),
        ))
        client = SimpleNamespace(configured=True, get=AsyncMock(return_value=SimpleNamespace(
            ok=False, status=503, payload=None,
        )))
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow, clash_client=client),
            deadline_monotonic=None,
        )
        self.assertEqual((await run_api(context, {})).status, "complete")
        workflow.check_clash_connection.reset_mock()
        with patch("elbow_helper.features.agent.datasets.clash.require_evidence_access", AsyncMock()):
            result = await read_clash(context, {"kind": "capital_raids", "tags": ["#P0"]})
        client.get.assert_awaited_once()
        self.assertEqual(client.get.await_args.args[0], "/clans/%23P0/capitalraidseasons?limit=10")
        self.assertEqual(result["items"], [{"tag": "#P0", "status": "maintenance", "http_status": 503}])
        self.assertEqual(result["flags"]["status"], "partial")
        workflow.check_clash_connection.assert_not_awaited()

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
