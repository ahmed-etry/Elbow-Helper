"""Live Clash reads use a fake application client."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.datasets.clash import ENDPOINTS, clash_tools, read_clash, summary
from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import encode_clash_tag


class ClashReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.access = patch(
            "elbow_helper.features.agent.datasets.clash.require_evidence_access", AsyncMock(),
        )
        self.require_access = self.access.start()
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
            if "/%23P2" in path:
                await asyncio.Event().wait()
            return SimpleNamespace(ok=True, status=200, payload={"name": "Synthetic"})

        self.client.get.side_effect = get
        for kind in ("player", "battle_log"):
            with self.subTest(kind=kind), patch(
                "elbow_helper.features.agent.datasets.clash.budgets.TOOL_TIMEOUT_SECONDS", 1.02,
            ):
                result = await read_clash(self.context, {"kind": kind, "tags": ["#P0", "#P2"]})
                self.assertEqual([item["status"] for item in result["items"]], ["ok", "unavailable"])

    async def test_battle_log_is_registered_and_uses_account_tags_without_clan_aliases(self):
        kinds = clash_tools()[0].definition.parameters["properties"]["kind"]["enum"]
        self.assertIn("battle_log", kinds)
        with patch("elbow_helper.features.agent.datasets.clash.CLANS", {"P0": SimpleNamespace(tag="#Q0")}):
            result = await read_clash(self.context, {"kind": "battle_log", "tags": ["p0"]})
        self.assertEqual(self.client.get.await_args.args[0], "/players/%23P0/battlelog")
        self.assertEqual(self.client.get.await_args.kwargs["attempts"], 1)
        self.assertEqual(result["items"][0]["tag"], "#P0")
        self.assertEqual(self.require_access.await_count, 2)

    async def test_battle_log_summary_preserves_modes_directions_losses_and_timestamps(self):
        entries = [{"battleType": mode, "attack": attack, "stars": 0,
                    "battleTimestamp": "20261008T120000.000Z", "battleTime": 120,
                    "opponentPlayerTag": "#Q0", "opponentName": "Synthetic opponent",
                    "destructionPercentage": 10, "army": [{"synthetic": True}]}
                   for mode, attack in (("RANKED", True), ("LEGEND", False), ("HOME_VILLAGE", True))]
        self.client.get.return_value.payload = {"items": entries}
        result = await read_clash(self.context, {"kind": "battle_log", "tags": ["#P0"]})
        self.assertEqual(result["items"][0]["status"], "ok")
        self.assertEqual(result["items"][0]["data"]["items"],
                         [{key: value for key, value in entry.items() if key != "army"} for entry in entries])
        self.assertIn("observed_at", result)

    async def test_battle_log_limits_are_applied_locally_for_summary_and_full_detail(self):
        entries = [{"battleType": "RANKED", "attack": True,
                    "opponentPlayerTag": "#Q0", "stars": index % 4, "army": [index]}
                   for index in range(12)]
        payload = {"items": entries}
        self.client.get.return_value.payload = payload
        for detail, limit in (("summary", None), ("summary", 3), ("full", 2)):
            with self.subTest(detail=detail, limit=limit):
                arguments = {"kind": "battle_log", "tags": ["#P0"], "detail": detail}
                if limit is not None:
                    arguments["limit"] = limit
                result = await read_clash(self.context, arguments)
                returned = result["items"][0]["data"]["items"]
                self.assertEqual(len(returned), 10 if limit is None else limit)
                self.assertEqual(self.client.get.await_args.args[0], "/players/%23P0/battlelog")
                if detail == "full":
                    self.assertEqual(returned, entries[:limit])
                else:
                    self.assertTrue(all("army" not in entry for entry in returned))
        self.assertEqual(payload["items"], entries)
        self.assertEqual(len(payload["items"]), 12)

    async def test_battle_log_empty_and_partial_entries_keep_available_data(self):
        for entries in ([], [{"battleType": "RANKED", "attackRenamed": True}]):
            with self.subTest(entries=entries):
                self.client.get.return_value.payload = {"items": entries}
                result = await read_clash(self.context, {"kind": "battle_log", "tags": ["#P0"]})
                self.assertEqual(result["items"][0]["data"]["items"],
                                 [] if not entries else [{"battleType": "RANKED"}])

    async def test_battle_log_access_denial_stops_the_request(self):
        self.require_access.side_effect = RuntimeError("synthetic access denied")
        with self.assertRaisesRegex(RuntimeError, "synthetic access denied"):
            await read_clash(self.context, {"kind": "battle_log", "tags": ["#P0"]})
        self.client.get.assert_not_awaited()

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
