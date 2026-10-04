"""Live profiles and nested account references use synthetic identities only."""

import asyncio
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote

from features.agent.engine.test_agent_plan_flow import _context, _Model, _Session, _model_step, _plan
from features.agent.result_path_helpers import assert_result_paths
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.capabilities.account_links.players import (
    PLAYER_CONCURRENCY, player_tools, read_live_players,
)
from elbow_helper.features.agent.capabilities.account_links.role_report import RoleAccountReport
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.plan.checker import check_entity_references, check_plan
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
from elbow_helper.infrastructure.clash import ClashClient, ClashResponse


def _profile(tag, townhall=15, clan=None):
    return ClashResponse(200, {"tag": tag, "name": "Synthetic player",
                               "townHallLevel": townhall, "clan": clan}, {}, 1, 1)


class LivePlayerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = ClashClient("synthetic-key")
        self.context = replace(_context(), account_links=SimpleNamespace(clash_client=self.client))

    async def test_shared_client_normalization_counts_and_inclusive_filters(self):
        responses = [_profile("#P0", 14), _profile("#P2", 15, {"tag": "#Q0", "name": "Synthetic clan"}),
                     _profile("#P8", 15), _profile("#P9", 16)]
        for filters, expected, counts in (
            ({}, ["#P0", "#P2", "#P8", "#P9"], {"14": 1, "15": 2, "16": 1}),
            ({"min_townhall": 15, "max_townhall": 15}, ["#P2", "#P8"], {"15": 2}),
            ({"max_townhall": 14}, ["#P0"], {"14": 1}),
            ({"min_townhall": 16}, ["#P9"], {"16": 1}),
            ({"min_townhall": 17}, [], {}),
        ):
            with self.subTest(filters=filters), patch.object(self.client, "get", AsyncMock(side_effect=responses)) as get:
                result = await read_live_players(self.context, {
                    "player_tags": ["po", "#p2", "P8", "#P9", "#P0"], **filters,
                })
                self.assertEqual([row["player_tag"] for row in result["accounts"]], expected)
                self.assertEqual(result["summary"], {"requested_accounts": 4, "found_accounts": 4,
                                                    "matching_accounts": len(expected), "counts_by_townhall": counts})
                self.assertEqual([call.args[0] for call in get.await_args_list],
                                 ["/players/%23P0", "/players/%23P2", "/players/%23P8", "/players/%23P9"])
                self.assertTrue(all(not call.kwargs for call in get.await_args_list))
                for row in result["accounts"]:
                    self.assertIsNotNone(datetime.fromisoformat(row["observed_at"]).utcoffset())
                    self.assertEqual(row["current_clan"],
                                     {"tag": "#Q0", "name": "Synthetic clan"} if row["player_tag"] == "#P2" else None)
                assert_result_paths(self, "read_live_players", result)

    async def test_missing_transient_and_invalid_profiles_preserve_successes(self):
        responses = [_profile("#P0"), ClashResponse(404, {}, {}, 1, 1),
                     ClashResponse(503, {}, {}, 3, 1),
                     ClashResponse(None, None, {}, 3, 1, TimeoutError()),
                     _profile("#QQ"), _profile("#PL", clan={"name": "Incomplete"}),
                     ClashResponse(200, {"tag": "#PG", "name": "Synthetic player"}, {}, 1, 1)]
        with patch.object(self.client, "get", AsyncMock(side_effect=responses)):
            result = await read_live_players(self.context, {"player_tags": ["#P0", "#P2", "#P8", "#P9", "#PY", "#PL", "#PG"]})
        self.assertEqual(result["summary"]["found_accounts"], 1)
        self.assertEqual(result["not_found_tags"], ["#P2"])
        self.assertEqual(result["unavailable_tags"], ["#P8", "#P9", "#PY", "#PL", "#PG"])

    async def test_two_hundred_tags_have_bounded_concurrency(self):
        active = maximum = 0
        async def get(path):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1
            return _profile(unquote(path.rsplit("/", 1)[1]))
        tags = ["#P" + format(index, "08b").replace("1", "2") for index in range(200)]
        with patch.object(self.client, "get", AsyncMock(side_effect=get)):
            result = await read_live_players(self.context, {"player_tags": tags})
        self.assertEqual(result["summary"]["counts_by_townhall"], {"15": 200})
        self.assertEqual(maximum, PLAYER_CONCURRENCY)
        self.assertEqual(active, 0)

    async def test_batch_timeout_retains_results_and_cancels_pending_work(self):
        stopped = []
        async def get(path):
            tag = unquote(path.rsplit("/", 1)[1])
            if tag == "#P0":
                return _profile(tag)
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(tag)
        with (patch.object(self.client, "get", AsyncMock(side_effect=get)),
              patch("elbow_helper.features.agent.capabilities.account_links.players.budgets.TOOL_TIMEOUT_SECONDS", 1.02)):
            result = await read_live_players(self.context, {"player_tags": ["#P0", "#P2", "#P8"]})
        self.assertEqual(result["unavailable_tags"], ["#P2", "#P8"])
        self.assertEqual(result["summary"]["found_accounts"], 1)
        self.assertCountEqual(stopped, ["#P2", "#P8"])

    async def test_invalid_inputs_never_call_client(self):
        for arguments in ({"player_tags": []}, {"player_tags": ["#P0"] * 201},
                          {"player_tags": ["invalid"]}, {"player_tags": [True]},
                          {"player_tags": ["#P0"], "min_townhall": 16, "max_townhall": 15},
                          {"player_tags": ["#P0"], "min_townhall": True}):
            with self.subTest(arguments=arguments), patch.object(self.client, "get", AsyncMock()) as get:
                with self.assertRaises(ValueError):
                    await read_live_players(self.context, arguments)
                get.assert_not_awaited()

    async def test_access_is_rechecked_after_live_read(self):
        async def get(path):
            self.context.member.roles.clear()
            return _profile("#P0")
        with patch.object(self.client, "get", AsyncMock(side_effect=get)):
            with self.assertRaises(AgentAccessLost):
                await read_live_players(self.context, {"player_tags": ["#P0"]})

    async def test_role_report_nested_reference_runs_through_agent(self):
        registry = build_agent_tools()
        members = tuple({"member_id": member_id, "display_name": "Synthetic member", "accounts": [
            {"player_tag": tag, "location_status": "unknown"} for tag in tags
        ]} for member_id, tags in ((101, ["#P0", "#P2"]), (202, []), (303, ["#P8"])))
        report = RoleAccountReport("synthetic-role-report", "2026-01-01T00:00:00+00:00", (), members)
        self.context.state.reports[report.report_id] = report
        reference = {"step": "roles", "path": ["members", "*", "accounts", "*", "player_tag"]}
        plan = _plan([
            {"id": "roles", "capability": "read_saved_report", "arguments": {
                "report_kind": "role_accounts", "report_id": report.report_id,
            }, "reason": "Read synthetic role account links", "depends_on": []},
            {"id": "profiles", "capability": "read_live_players", "arguments": {"player_tags": reference},
             "reason": "Look up current synthetic profiles", "depends_on": ["roles"]},
        ])
        checked = check_plan(plan, registry)
        self.assertTrue(checked.ok, checked.error)
        session = _Session([_model_step(plan), AgentStep("Synthetic lookup answer", (), AgentUsage())], [])
        async def get(path):
            return _profile(unquote(path.rsplit("/", 1)[1]))
        with patch.object(self.client, "get", AsyncMock(side_effect=get)) as get:
            answer = await AgentService(_Model(session)).answer(question="Inspect synthetic profiles", local_context="", context=self.context)
        self.assertEqual(answer, "Synthetic lookup answer")
        self.assertEqual(get.await_count, 3)
        self.assertIn('"15": 3', session.calls[-1][0][0].content)
        self.assertTrue(self.context.state.evidence)

    def test_live_tool_advertises_account_kind(self):
        tool = player_tools()[0]
        self.assertEqual(tool.contract.result_path_kind(["accounts", "*", "player_tag"]), "clash_account")
        self.assertIn("Live Clash", tool.definition.description)

    def test_nested_reference_keeps_entity_kind_and_scalar_checks(self):
        registry = build_agent_tools()
        steps = {"roles": {"capability": "read_saved_report", "arguments": {"report_kind": "role_accounts"}}}
        reference = {"step": "roles", "path": ["members", "*", "accounts", "*", "player_tag"]}
        checked = check_entity_references(registry["read_live_players"].contract,
                                         {"player_tags": reference}, steps, registry)
        self.assertTrue(checked.ok, checked.error)
        reference["path"] = ["members", "*", "member_id"]
        self.assertFalse(check_entity_references(registry["read_live_players"].contract,
                                                {"player_tags": reference}, steps, registry).ok)
        reference["path"] = ["members", "*", "accounts", "*", "player_tag"]
        self.assertFalse(check_entity_references(registry["get_account_link"].contract,
                                                {"player_tag": reference}, steps, registry).ok)

