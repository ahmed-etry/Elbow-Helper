"""Live Clash reads use a fake application client."""
import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.reads.clash import ENDPOINTS, read_clash
from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import encode_clash_tag
from elbow_helper.agent.engine.tool_call import bound_tool_result
from elbow_helper.agent.plan.results import model_result, model_view
from elbow_helper.agent.tool_support.clash_cache import RequestClashReads


class ClashReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.access = patch(
            "elbow_helper.agent.reads.clash.require_evidence_access", AsyncMock(),
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
                    expected += "?limit=" + ("10" if kind == "capital_raids" else "50")
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


    async def test_player_clan_evidence_distinguishes_clanless_from_failed_reads(self):
        clan = {"tag": "#Q0", "name": "Synthetic clan"}
        for projection in ({}, {"detail": "full"}, {"fields": ["name", "clan"]},
                           {"fields": ["clan.tag", "clan.name"]}):
            for membership, expected_status in (({"clan": clan}, "in_clan"), ({}, "not_in_clan"),
                                                ({"clan": None}, "not_in_clan")):
                with self.subTest(projection=projection, membership=membership):
                    payload = {"tag": "#P0", "name": "Synthetic", **membership}
                    self.client.get.return_value = SimpleNamespace(ok=True, status=200, payload=payload)
                    result = await read_clash(self.context, {"kind": "player", "tags": ["#P0"],
                                                            **projection})
                    item = model_view(model_result(result))["items"][0]
                    self.assertEqual(item["status"], "ok")
                    self.assertEqual(item["data"].get("clan"), membership.get("clan"))
                    self.assertEqual(item["data"]["clan_status"], expected_status)
                    if "clan" not in membership:
                        self.assertNotIn("clan", item["data"])
                    self.assertEqual(payload, {"tag": "#P0", "name": "Synthetic", **membership})
            for status in (404, 503, 500):
                with self.subTest(projection=projection, failed_status=status):
                    self.client.get.return_value = SimpleNamespace(ok=False, status=status, payload=None)
                    result = await read_clash(self.context, {"kind": "player", "tags": ["#P0"],
                                                            **projection})
                    item = model_view(model_result(result))["items"][0]
                    self.assertNotEqual(item["status"], "ok")
                    self.assertNotIn("data", item)
                    self.assertNotIn("clan_status", item)


    async def test_clanless_evidence_survives_cache_reuse_without_mutating_raw_profiles(self):
        self.enable_cache()
        payload = {"tag": "#P0", "name": "Synthetic"}
        self.client.get.return_value.payload = payload
        args = {"kind": "player", "tags": ["#P0"]}
        first = await read_clash(self.context, {**args, "detail": "full"})
        first["items"][0]["data"]["clan_status"] = "Changed by a consumer"
        for projection in ({}, {"fields": ["clan"]}):
            result = await read_clash(self.context, {**args, **projection})
            self.assertTrue(result["items"][0]["reused"])
            self.assertNotIn("clan", result["items"][0]["data"])
            self.assertEqual(result["items"][0]["data"]["clan_status"], "not_in_clan")
        self.client.get.assert_awaited_once()
        self.assertEqual(payload, {"tag": "#P0", "name": "Synthetic"})

    async def test_timeout_preserves_completed_results(self):
        async def get(path, **kwargs):
            if "/%23P2" in path:
                await asyncio.Event().wait()
            return SimpleNamespace(ok=True, status=200, payload={"name": "Synthetic"})

        self.client.get.side_effect = get
        for kind in ("player", "battle_log"):
            with self.subTest(kind=kind), patch(
                "elbow_helper.agent.reads.clash.budgets.TOOL_TIMEOUT_SECONDS", 1.02,
            ):
                result = await read_clash(self.context, {"kind": kind, "tags": ["#P0", "#P2"]})
                self.assertEqual([item["status"] for item in result["items"]], ["ok", "timeout"])


    async def test_roster_get_retries_transient_failure_without_losing_successful_siblings(self):
        from elbow_helper.infrastructure.clash import ClashClient
        from core.test_clash_infrastructure import _FakeResponse, _FakeSession
        client = ClashClient('synthetic-key')
        session = _FakeSession([_FakeResponse(503, {}), _FakeResponse(200, {
            'tag': '#P0', 'members': 1, 'memberList': [{'tag': '#Q0'}]})])
        client._session = session
        self.context.bot.clash_client = client
        try:
            result = await read_clash(self.context, {'kind': 'clan', 'tags': ['#P0'], 'detail': 'full'})
        finally:
            await client.close()
        self.assertEqual(result['items'][0]['status'], 'ok')
        self.assertEqual(result['items'][0]['data']['members'], 1)
        self.assertEqual(len(session.urls), 2)

    async def test_failed_task_is_explicit_and_does_not_hide_other_clan_results(self):
        async def get(path, **kwargs):
            if path.endswith('%23P2'):
                raise ValueError('Synthetic transport failure')
            return SimpleNamespace(ok=True, status=200, payload={'members': 1})
        self.client.get.side_effect = get
        result = await read_clash(self.context, {'kind': 'clan', 'tags': ['#P0', '#P2']})
        self.assertEqual(result['items'][0]['status'], 'ok')
        self.assertEqual(result['items'][1]['error_type'], 'ValueError')
        self.assertEqual(result['flags']['status'], 'partial')

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
                item = result["items"][0]
                self.assertTrue(item["truncated"])
                self.assertEqual(item["row_counts"]["/items"], {
                    "returned_rows": len(returned), "total_rows": 12,
                })
                self.assertEqual(model_result(result)["flags"]["status"], "partial")
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


    async def test_chronological_selection_finds_recent_attack_in_any_api_order(self):
        entries = [{"battleTimestamp": f"202610{day:02d}T120000.000Z", "attack": day == 18,
                    "stars": day % 4, "army": [day]} for day in range(1, 19)]
        for rows in (entries, entries[::-1], entries[::2] + entries[1::2]):
            self.client.get.return_value.payload = {"items": rows}
            result = await read_clash(self.context, {"kind": "battle_log", "tags": ["#P0"], "limit": 12})
            item = result["items"][0]
            self.assertTrue(item["data"]["items"][0]["attack"])
            self.assertEqual(item["coverage"]["available_range"]["oldest"], "2026-10-01T12:00:00+00:00")
            self.assertEqual(item["coverage"]["returned_range"]["oldest"], "2026-10-07T12:00:00+00:00")
            self.assertEqual(item["coverage"]["next_offset"], 12)
            self.assertFalse(item["coverage"]["selection_complete"])
            older = await read_clash(self.context, {"kind": "battle_log", "tags": ["#P0"],
                                                    "limit": 2, "order": "oldest_first", "detail": "full"})
            self.assertEqual(older["items"][0]["data"]["items"], entries[:2])
            window = await read_clash(self.context, {"kind": "battle_log", "tags": ["#P0"],
                "after": "2026-10-17", "before": "2026-10-18", "offset": 0})
            self.assertEqual(window["items"][0]["coverage"]["matching_rows"], 1)

    async def test_unknown_timestamps_and_empty_date_slice_have_explicit_coverage(self):
        self.client.get.return_value.payload = {"items": [{"battleTimestamp": "bad", "attack": True}]}
        result = await read_clash(self.context, {"kind": "battle_log", "tags": ["#P0"], "after": "2026-10-01"})
        item = result["items"][0]
        self.assertEqual(item["data"]["items"], [])
        self.assertEqual(item["coverage"]["unknown_timestamp_rows"], 1)
        self.assertFalse(item["coverage"]["chronology_complete"])
        self.assertEqual(item["coverage"]["returned_range"], {"oldest": None, "newest": None})

    def enable_cache(self):
        self.context.clash_reads = RequestClashReads()
        self.context.guild = SimpleNamespace(id=1)
        self.context.member = SimpleNamespace(id=2)

    @patch('elbow_helper.agent.reads.clash.datetime')
    async def test_live_battle_log_batches_and_nested_ranked_reads_always_fetch(self, clock):
        clock.now.side_effect = (datetime(2026, 10, 1, 0, 0, second, tzinfo=timezone.utc) for second in range(20))
        self.enable_cache()
        self.client.get.return_value.payload = {"items": [
            {"battleTimestamp": f"202610{day:02d}T120000.000Z", "army": [day], "attack": True}
            for day in range(1, 19)]}
        result = await read_clash(self.context, {"kind": "battle_log", "tags": ["#P0", "#P2"], "limit": 12})
        bounded = bound_tool_result(json.dumps(result), 1000)
        self.assertEqual(json.loads(bounded)["items"], [])
        full = await read_clash(self.context, {"kind": "battle_log", "tags": ["p2"], "detail": "full", "limit": 2})
        item = full["items"][0]
        self.assertFalse(item["reused"])
        self.assertEqual(item["data"]["items"][0]["army"], [18])
        self.assertEqual(item["row_counts"]["/items"], {"returned_rows": 2, "total_rows": 18})
        self.assertEqual(self.client.get.await_count, 3)
        self.assertNotEqual(full["observed_at"], result["items"][1]["observed_at"])
        item["data"]["items"][0]["army"].append("mutated")
        repeated = await read_clash(self.context, {"kind": "battle_log", "tags": ["#P2"], "detail": "full"})
        self.assertEqual(repeated["items"][0]["data"]["items"][0]["army"], [18])


    async def test_size_caps_return_no_excerpt_and_preserve_api_row_totals(self):
        for kind in ENDPOINTS:
            with self.subTest(kind=kind):
                # Extra fields make every full dataset exceed the encoded size cap.
                self.client.get.return_value.payload = {
                    "items": [{"stars": 0, "details": '"\\\n' * 1000} for _ in range(12)],
                }
                result = await read_clash(self.context, {
                    "kind": kind, "tags": ["#P0"], "detail": "full", "limit": 3,
                })
                content = json.dumps(model_view(model_result(result)), ensure_ascii=False)
                bounded = bound_tool_result(content, 1000)
                self.assertLessEqual(len(bounded), 1000)
                view = json.loads(bounded)
                self.assertTrue(view["truncated"])
                self.assertNotIn("result_excerpt", view)
                if "items" not in view:
                    self.assertEqual(view["total_rows"], 12)
                elif view["items"]:
                    counts = view["items"][0]["row_counts"]["/items"]
                    self.assertEqual(counts["total_rows"], 12)
                    self.assertLessEqual(counts["returned_rows"], 3)
                else:
                    self.assertEqual(view["row_counts"][0]["collections"]["/items"], {
                        "returned_rows": 0, "total_rows": 12,
                    })


    async def test_size_cap_counts_survive_compacted_account_table_and_tiny_budget(self):
        self.client.get.return_value.payload = {"items": [{"stars": 0, "details": "x" * 2000}] * 12}
        result = await read_clash(self.context, {"kind": "battle_log", "tags": ["#P0"] * 10})
        content = json.dumps(model_view(model_result(result)))
        self.assertIn("columns", json.loads(content)["items"])
        view = json.loads(bound_tool_result(content, 100))
        self.assertEqual(view, {"truncated": True, "returned_rows": 0, "total_rows": 120})
