"""Live Clash reads use a fake application client."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.datasets.clash import ENDPOINTS, clash_tools, read_clash, summary
from elbow_helper.configuration.clans import CLANS
from elbow_helper.domain.player_tags import encode_clash_tag
from elbow_helper.features.agent.engine.tool_call import bound_tool_result
from elbow_helper.features.agent.plan.results import model_result, model_view
from elbow_helper.features.agent.datasets.clash_cache import RequestClashReads
from elbow_helper.features.agent.datasets.ranked import _live_batch
from elbow_helper.features.agent.models import AgentRequestContext, AgentTurnState


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

    async def test_uncut_and_empty_logs_are_not_marked_truncated(self):
        for count in (0, 3, 10):
            with self.subTest(count=count):
                self.client.get.return_value.payload = {"items": [{"stars": 0}] * count}
                result = await read_clash(self.context, {"kind": "battle_log", "tags": ["#P0"]})
                self.assertFalse(result["truncated"])
                self.assertEqual(result["items"][0]["row_counts"]["/items"], {
                    "returned_rows": count, "total_rows": count,
                })
                self.assertFalse(result["items"][0]["coverage"]["history_complete"])
                self.assertEqual(model_result(result)["flags"]["status"], "partial")

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

    async def test_batches_subsets_details_and_nested_ranked_reads_reuse_raw_accounts(self):
        self.enable_cache()
        self.client.get.return_value.payload = {"items": [
            {"battleTimestamp": f"202610{day:02d}T120000.000Z", "army": [day], "attack": True}
            for day in range(1, 19)]}
        result = await read_clash(self.context, {"kind": "battle_log", "tags": ["#P0", "#P2"], "limit": 12})
        bounded = bound_tool_result(json.dumps(result), 1000)
        self.assertEqual(json.loads(bounded)["items"], [])
        full = await read_clash(self.context, {"kind": "battle_log", "tags": ["p2"], "detail": "full", "limit": 2})
        item = full["items"][0]
        self.assertTrue(item["reused"])
        self.assertEqual(item["data"]["items"][0]["army"], [18])
        self.assertEqual(item["row_counts"]["/items"], {"returned_rows": 2, "total_rows": 18})
        nested = await _live_batch(self.context, "battle_log", ["#P0", "#P2"])
        self.assertEqual(len(nested["items"][0]["data"]["items"]), 18)
        self.assertEqual(self.client.get.await_count, 2)
        self.assertEqual(full["observed_at"], result["items"][1]["observed_at"])
        item["data"]["items"][0]["army"].append("mutated")
        repeated = await read_clash(self.context, {"kind": "battle_log", "tags": ["#P2"], "detail": "full"})
        self.assertEqual(repeated["items"][0]["data"]["items"][0]["army"], [18])

    async def test_concurrent_reads_coalesce_per_account(self):
        self.enable_cache()
        async def fetch(*args, **kwargs):
            await asyncio.sleep(0)
            return SimpleNamespace(ok=True, status=200, payload={"items": []})
        self.client.get.side_effect = fetch
        await asyncio.gather(*(read_clash(self.context, {"kind": "battle_log", "tags": ["#P0"], "limit": limit})
                               for limit in (12, 25, 50)))
        self.client.get.assert_awaited_once()

    async def test_cache_is_shared_by_request_clones_and_isolated_from_next_request(self):
        context = AgentRequestContext(bot=self.context.bot, guild=SimpleNamespace(id=1),
            member=SimpleNamespace(id=2), source_message=None, account_links=None, message_search=None)
        args = {"kind": "player", "tags": ["#P0"]}
        await read_clash(context, args)
        clone = replace(context, state=AgentTurnState())
        await read_clash(clone, {**args, "detail": "full"})
        self.client.get.assert_awaited_once()
        next_request = AgentRequestContext(bot=context.bot, guild=context.guild,
            member=context.member, source_message=None, account_links=None, message_search=None)
        await read_clash(next_request, args)
        self.assertEqual(self.client.get.await_count, 2)

    async def test_cache_freshness_scope_access_and_size_bounds(self):
        self.enable_cache()
        args = {"kind": "player", "tags": ["#P0"]}
        now = [0]
        with patch("elbow_helper.features.agent.datasets.clash_cache.monotonic", side_effect=lambda: now[0]):
            await read_clash(self.context, args)
            now[0] = 2
            await read_clash(self.context, args)
            self.assertEqual(self.client.get.await_count, 1)
            await read_clash(self.context, {**args, "max_age_seconds": 1})
            self.assertEqual(self.client.get.await_count, 2)
            now[0] = 32
            await read_clash(self.context, args)
            self.assertEqual(self.client.get.await_count, 3)
            await read_clash(self.context, {**args, "max_age_seconds": 0})
            self.assertEqual(self.client.get.await_count, 4)
        self.context.member.id = 3
        await read_clash(self.context, args)
        self.assertEqual(self.client.get.await_count, 5)
        self.require_access.side_effect = RuntimeError("access lost")
        with self.assertRaises(RuntimeError):
            await read_clash(self.context, args)
        self.assertFalse(self.context.clash_reads.entries)
        self.require_access.side_effect = None
        with patch("elbow_helper.features.agent.datasets.clash_cache.MAX_BYTES", 1):
            await read_clash(self.context, args)
            await read_clash(self.context, args)
        self.assertEqual(self.client.get.await_count, 7)
        self.assertFalse(self.context.clash_reads.entries)

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
                self.assertEqual(view["items"], [])
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
        nested = json.dumps({"accounts": [], "live_battle_logs": {"result": model_result(result)}})
        self.assertEqual(json.loads(bound_tool_result(nested, 100)), view)
