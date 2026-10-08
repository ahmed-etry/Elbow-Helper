"""Live Clash reads use a fake application client."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.datasets.clash import ENDPOINTS, read_clash, summary
from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import encode_clash_tag


class ClashReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.access = patch(
            "elbow_helper.features.agent.datasets.clash.require_evidence_access", AsyncMock(),
        )
        self.access.start()
        self.addCleanup(self.access.stop)
        self.client = SimpleNamespace(configured=True, get=AsyncMock(return_value=SimpleNamespace(
            ok=True, status=200, payload={"tag": "#P0", "name": "Synthetic"},
        )))
        self.context = SimpleNamespace(
            bot=SimpleNamespace(clash_client=self.client), deadline_monotonic=None,
        )

    async def test_paths_and_clan_codes(self):
        for kind, endpoint in ENDPOINTS.items():
            with self.subTest(kind=kind):
                await read_clash(self.context, {"kind": kind, "tags": ["#P0"]})
                expected = endpoint.format(tag="%23P0")
                if kind in ("capital_raids", "war_log"):
                    expected += "?limit=" + ("2" if kind == "capital_raids" else "10")
                self.assertEqual(self.client.get.await_args.args[0], expected)
        code = next(iter(CLANS))
        await read_clash(self.context, {"kind": "clan", "tags": [code]})
        self.assertEqual(
            self.client.get.await_args.args[0], "/clans/" + encode_clash_tag(CLANS[code].tag),
        )
        self.client.get.reset_mock()
        result = await read_clash(self.context, {"kind": "cwl_war", "tags": ["#0"]})
        self.assertEqual(result["items"][0]["status"], "not_found")
        self.client.get.assert_not_awaited()

    async def test_statuses_and_limits(self):
        for kind in ENDPOINTS:
            for code, expected in (
                (404, "not_found"), (503, "maintenance"), (500, "unavailable"),
                (403, "private"
                 if kind in ("current_war", "cwl_group", "war_log") else "unavailable"),
            ):
                self.client.get.return_value = SimpleNamespace(ok=False, status=code, payload=None)
                self.assertEqual(
                    (await read_clash(self.context, {"kind": kind, "tags": ["#P0"]}))
                    ["items"][0]["status"], expected,
                )
            cap = 200 if kind == "player" else 20
            self.assertIn("error", await read_clash(
                self.context, {"kind": kind, "tags": ["#P0"] * (cap+1)},
            ))
        self.assertIn("error", await read_clash(
            self.context, {"kind": "player", "detail": "full", "tags": ["#P0"] * 6},
        ))

    async def test_timeout_preserves_completed_results(self):
        async def get(path, **kwargs):
            if path.endswith("%23P2"):
                await asyncio.Event().wait()
            return SimpleNamespace(ok=True, status=200, payload={"name": "Synthetic"})

        self.client.get.side_effect = get
        with patch("elbow_helper.features.agent.datasets.clash.budgets.TOOL_TIMEOUT_SECONDS", 1.02):
            result = await read_clash(self.context, {"kind": "player", "tags": ["#P0", "#P2"]})
        self.assertEqual([item["status"] for item in result["items"]], ["ok", "unavailable"])

    def test_projection_drops_unrequested_data(self):
        result = summary("clan", {
            "tag": "#P0", "description": "x"*400, "secret": 1,
            "memberList": [{"tag": "#P2", "name": "Synthetic", "secret": 2}],
        })
        self.assertNotIn("secret", result)
        self.assertEqual(len(result["description"]), 300)
        self.assertNotIn("secret", result["memberList"][0])
        self.assertEqual(
            summary("capital_raids", {"items": [{
                "startTime": "synthetic", "attackLog": [1], "defenseLog": [2],
            }]}),
            {"items": [{"startTime": "synthetic"}]},
        )
