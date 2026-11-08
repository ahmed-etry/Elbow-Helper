"""Clash connection checks share one feature operation."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.agent.features.diagnostics.commands import run_api
from elbow_helper.agent.reads.clash import read_clash
from elbow_helper.features.diagnostics.cog import ClashConnectionCheck


class DiagnosticCommandTests(unittest.IsolatedAsyncioTestCase):

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
        with patch("elbow_helper.agent.reads.clash.require_evidence_access", AsyncMock()):
            result = await read_clash(context, {"kind": "capital_raids", "tags": ["#P0"]})
        client.get.assert_awaited_once()
        self.assertEqual(client.get.await_args.args[0], "/clans/%23P0/capitalraidseasons?limit=10")
        self.assertEqual(result["items"], [{"tag": "#P0", "status": "maintenance", "http_status": 503}])
        self.assertEqual(result["flags"]["status"], "partial")
        workflow.check_clash_connection.assert_not_awaited()
