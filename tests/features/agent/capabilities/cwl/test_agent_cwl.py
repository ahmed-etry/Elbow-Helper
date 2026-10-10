from datetime import datetime, timezone
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from unittest.mock import patch

from elbow_helper.configuration.roles import CORE, CO_APPLICANT_ROLE_ID
from elbow_helper.features.agent.capabilities.cwl.report import CwlPerformanceReport
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.models import AgentRequestContext
from features.agent.report_helpers import make_event_report
from elbow_helper.features.agent.capabilities.cwl.reads import read_cwl_performance, read_cwl_performance_report, read_cwl_threads
from elbow_helper.features.agent.capabilities.cwl.scoring import cwl_ass_scores, cwl_bonus_scores
from elbow_helper.features.agent.files.spreadsheet_tools import prepare_spreadsheet
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.engine.budgets import MAX_TOOL_RESULT_CHARACTERS
from elbow_helper.features.agent.engine.tool_call import bound_tool_result
from elbow_helper.features.agent.plan.results import model_view
from elbow_helper.features.cwl.bonus.analysis import BonusAnalysisService
from elbow_helper.features.cwl.queries import CwlQueries
from elbow_helper.infrastructure.clash import ClashClient
from features.test_cwl_queries import _History, _BonusConfig, _dataset
from elbow_helper.infrastructure.exports import LocalExportStore, WorkbookWriter
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
from openpyxl import load_workbook
from features.agent.engine.test_agent_plan_flow import _Session, _Model, _model_step

from elbow_helper.features.cwl.queries import (
    CwlAssScopeRow, CwlAssWarsSnapshot, CwlBonusAttackScore,
    CwlBonusWarsSnapshot, CwlBonusSettings, CwlClanSeasonSummary,
    CwlPerformanceRow, CwlPerformanceSnapshot,
    CwlThreadRegistration,
)


def _snapshot(count=30):
    rows = tuple(CwlPerformanceRow(
        season="2026-08", clan_code="BEH", league="Champion League II",
        profile_key="high_2026_06", player_tag=f"#P{index}",
        player_name=f"Player {index:02}", townhall=18, wars=7, attacks=7,
        attacks_expected=7, stars=21, average_destruction=100.0, score=21.0,
        rank=index + 1, rank_total=count, multi_season_score=21.0,
        multi_season_rank=index + 1, multi_season_rank_total=count,
    ) for index in range(count))
    return CwlPerformanceSnapshot(
        "2026-09-17T12:00:00+00:00", 3, ("2026-08",),
        (CwlClanSeasonSummary("2026-08", "BEH", "Champion League II", 1007,
                              7, count, count * 7, count * 7, True),), rows,
    )


class _Queries:
    def __init__(self):
        self.snapshot = _snapshot()
        self.calls = []
        self.scopes = []
        self.threads = (
            CwlThreadRegistration("BEH", "Brown Elbow Heroes", "#P0", 100, "now"),
            CwlThreadRegistration("BEC", "Brown Elbow Clan", "#P2", 200, None),
        )

    def performance(self, *, history_limit, season=None, clan_code=None):
        self.calls.append(history_limit)
        self.scopes.append((season, clan_code))
        return self.snapshot

    def registered_threads(self):
        return self.threads


    def ass_wars(self, *, clan_code, war_ids):
        self.calls.append(("ass_wars", clan_code, war_ids))
        return CwlAssWarsSnapshot(
            "2026-09-17T12:00:00+00:00", clan_code, ("2026-08",),
            tuple(war_ids), (1,), 1, "Champion League II", "high_2026_06",
            "High League Standard (Jun 2026)", 0.4, "high_linear",
            "calculated_from_selected_scope", "selected_completed_wars",
            (CwlAssScopeRow(
                "#P0", "Alpha", 18, 1, 1, 1, 3, 100.0,
                1.0, 0.0, 1.0, 21.0, 1, 1, 21.0, 0.0, 0.0, 0.0,
            ),), ({"war_id": war_ids[0], "stars": 3},),
        )

    def bonus_wars(self, *, clan_code, war_ids):
        self.calls.append(("bonus_wars", clan_code, war_ids))
        return CwlBonusWarsSnapshot(
            "2026-09-17T12:00:00+00:00", clan_code, ("2026-08",),
            (1,), tuple(war_ids),
            CwlBonusSettings(4, "2026-08-01T00:00:00+00:00", 2, 8,
                             0.15, 0.10, 0, 0.20, 2.0),
            (), (), (CwlBonusAttackScore(
                1, war_ids[0], "#P0", "Alpha", 18,
                "#D", 18, 3, 100.0, 3.0, 2.0, 0, "18:18", 1.0,
                0.0, 1.0, 3, "",
            ),), (), "selected_scored_attacks", tuple(war_ids), len(war_ids),
        )


def _large_selection_queries():
    sample = _dataset()
    dataset = {"wars": [], "roster": [], "attacks": []}
    for month in range(1, 9):
        for round_number in range(1, 8):
            war_id = f"CWL:#WAR{month}{round_number}"
            dataset["wars"].append({
                **sample["wars"][0], "war_id": war_id, "cwl_season": f"2026-{month:02d}",
                "cwl_round": round_number, "team_size": 30, "state": "warEnded",
            })
            for index in range(30):
                player = {"player_tag": f"#P{index}", "player_name": f"Synthetic {index:02d}"}
                dataset["roster"].append({
                    **sample["roster"][0], **player, "war_id": war_id,
                    "map_position": index + 1, "attacks_used": 1,
                })
                dataset["attacks"].append({
                    **sample["attacks"][0], **player, "war_id": war_id,
                    "attack_order": index + 1, "defender_map_position": index + 1,
                    "defender_tag": f"#D{index}", "stars": 2 + index % 2,
                    "destruction": 80.5 + index % 2 * 19.5,
                })
    history = _History(dataset)
    queries = CwlQueries(
        history, lambda: {}, bonus_config=_BonusConfig(),
        bonus_analysis=BonusAnalysisService(ClashClient(None), history),
        clock=lambda: datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
    )
    return queries, [war["war_id"] for war in dataset["wars"]]


class AgentCwlTests(unittest.IsolatedAsyncioTestCase):

    async def test_ass_read_and_result_sheet_build_workbook_in_one_round(self):
        registry = build_agent_tools()
        registry = {name: registry[name] for name in ("cwl_ass_scores", "prepare_spreadsheet")}
        plan = {
            "goal": "Synthetic season scores", "effort": "low", "output": "prepare_spreadsheet",
            "steps": [
                {"id": "scores", "capability": "cwl_ass_scores", "depends_on": [],
                 "arguments": {"clan_code": "BE1", "war_ids": ["CWL:#WAR"]}},
                {"id": "workbook", "capability": "prepare_spreadsheet", "depends_on": ["scores"],
                 "arguments": {"title": "Synthetic season", "sheets": [{
                     "name": "Scores", "rows_from": {"step": "scores", "path": ["players"]},
                     "columns": [{"field": "player_name", "heading": "Account"},
                                 {"field": "ass_score", "heading": "ASS"}],
                 }]}},
            ],
        }
        session = _Session([
            _model_step(plan), AgentStep("Synthetic workbook ready", (), AgentUsage()),
        ], [])
        with TemporaryDirectory() as directory:
            self.context.bot.local_exports = LocalExportStore(Path(directory))
            self.context.bot.workbook_writer = WorkbookWriter()
            self.context.guild.filesize_limit = 8 * 1024 * 1024
            with patch(
                "elbow_helper.features.agent.engine.service.build_agent_tools",
                return_value=registry,
            ):
                answer = await AgentService(_Model(session)).answer(
                    question="Synthetic scores as a spreadsheet", local_context="",
                    context=self.context,
                )
            self.assertEqual(answer, "Synthetic workbook ready")
            self.assertEqual(len(session.calls), 2)
            results = json.loads(session.calls[1][0][0].content)["results"]
            player = results["scores"]["players"][0]
            self.assertTrue(results["workbook"]["attachment_prepared"])
            workbook = load_workbook(BytesIO(self.context.state.attachments[0].data))
            try:
                self.assertEqual(workbook["Scores"]["A2"].value, player["player_name"])
                self.assertEqual(workbook["Scores"]["B2"].value, player["ass_score"])
                self.assertEqual(workbook["Scores"]["B2"].data_type, "n")
            finally:
                workbook.close()
            self.assertIn(("ass_wars", "BE1", ["CWL:#WAR"]),
                          self.queries.calls)

    def setUp(self):
        member = SimpleNamespace(id=42, display_name="Tester", roles=[SimpleNamespace(id=next(iter(CORE))), SimpleNamespace(id=CO_APPLICANT_ROLE_ID)])
        guild = SimpleNamespace(id=1, name="Brown Elbow", me=member, get_member=lambda _: member)
        channel = SimpleNamespace(id=100, guild=guild, permissions_for=lambda _: SimpleNamespace(
            view_channel=True, read_message_history=True,
        ))
        guild.get_channel_or_thread = lambda value: channel if value == 100 else None
        self.queries = _Queries()
        self.context = AgentRequestContext(
            bot=SimpleNamespace(fetch_channel=AsyncMock(return_value=None)), guild=guild, member=member,
            source_message=SimpleNamespace(
                id=500, channel=channel, created_at=datetime.now(timezone.utc),
            ),
            account_links=None, message_search=None,
            cwl_queries=self.queries,
        )

    async def _assert_large_selection_keeps_scores(self, capability, row_field):
        queries, war_ids = _large_selection_queries()
        self.context = replace(self.context, cwl_queries=queries)
        plan = {"goal": "Synthetic scores", "effort": "low", "output": "text", "steps": [{
            "id": "scores", "capability": capability, "depends_on": [],
            "arguments": {"clan_code": "BEH", "war_ids": war_ids},
        }]}
        session = _Session([
            _model_step(plan), AgentStep("Synthetic scores ready", (), AgentUsage()),
        ], [])
        registry = build_agent_tools()
        with patch("elbow_helper.features.agent.engine.service.build_agent_tools",
                   return_value={capability: registry[capability]}):
            answer = await AgentService(_Model(session)).answer(
                question="Synthetic chosen war scores", local_context="", context=self.context,
            )
        self.assertEqual(answer, "Synthetic scores ready")
        record = json.loads(self.context.state.evidence[0])
        payload = json.loads(record["result"])
        self.assertEqual(next(iter(payload)), row_field)
        self.assertEqual(payload["completed_wars"], 56)
        self.assertEqual(set(payload["resolved_war_ids"]), set(war_ids))
        self.assertEqual(len(payload[row_field]), 30)
        self.assertEqual(len(payload["attack_sample"]), 56 * 30)
        self.assertTrue(all(row["attacks" if row_field == "players" else "attack_count"] == 56
                            for row in payload[row_field]))
        view = model_view(payload)
        content = json.dumps(view, ensure_ascii=False, default=str, separators=(",", ":"))
        bounded = bound_tool_result(content, MAX_TOOL_RESULT_CHARACTERS)
        self.assertLessEqual(len(bounded), MAX_TOOL_RESULT_CHARACTERS)
        excerpt = json.loads(bounded)["result_excerpt"]
        self.assertIn(json.dumps(view[row_field], ensure_ascii=False, default=str,
                                 separators=(",", ":")), excerpt)
        result = json.loads(session.calls[1][0][0].content)["results"]["scores"]
        self.assertEqual(result["result_excerpt"], excerpt)
        self.assertEqual(result["flags"]["status"], "partial")
        self.assertTrue(result["flags"]["truncated"])
        self.assertFalse(record["result_complete"])

    async def test_ass_keeps_all_30_player_scores_when_56_wars_exceed_the_result_limit(self):
        await self._assert_large_selection_keeps_scores("cwl_ass_scores", "players")

    async def test_bonus_keeps_all_30_player_scores_when_56_wars_exceed_the_result_limit(self):
        await self._assert_large_selection_keeps_scores("cwl_bonus_scores", "rows")

    async def test_complete_performance_report_is_retained_and_paged(self):
        first = await read_cwl_performance(self.context, {"history_limit": 3})
        self.assertEqual(self.queries.calls, [3])
        self.assertEqual(first["matching_rows"], 30)
        self.assertEqual(len(first["players"]), 25)
        report = self.context.state.reports[first["report_id"]]
        self.queries.snapshot = _snapshot(1)

        second = await read_cwl_performance_report(self.context, {
            "report_id": first["report_id"], "offset": first["next_offset"],
        })
        self.assertEqual(len(second["players"]), 5)
        self.assertEqual(second["matching_rows"], 30)
        self.assertIs(report, self.context.state.reports[first["report_id"]])
        self.assertEqual(self.queries.calls, [3])
        whole = await read_cwl_performance_report(self.context, {
            "report_id": first["report_id"], "limit": 100,
        })
        self.assertEqual(len(whole["players"]), 30)
        self.assertIsNone(whole["next_offset"])

    async def test_performance_source_receives_requested_period_and_clan(self):
        await read_cwl_performance(self.context, {
            "season": "2026-08", "clan_code": "BEH", "player_tag": "#P0",
        })

        self.assertEqual(self.queries.scopes, [("2026-08", "BEH")])

    async def test_report_filters_and_rejects_invalid_tag_or_kind(self):
        first = await read_cwl_performance(self.context, {})
        filtered = await read_cwl_performance_report(self.context, {
            "report_id": first["report_id"], "player_tag": "P0",
        })
        self.assertEqual(filtered["matching_rows"], 1)
        self.assertEqual(filtered["players"][0]["player_tag"], "#P0")
        invalid = await read_cwl_performance_report(self.context, {
            "report_id": first["report_id"], "player_tag": "BAD",
        })
        self.assertIn("error", invalid)
        self.context.state.reports["wrong"] = make_event_report("wrong", "now")
        self.assertIn("error", await read_cwl_performance_report(
            self.context, {"report_id": "wrong"},
        ))

    async def test_registered_threads_include_only_currently_accessible_sources(self):
        result = await read_cwl_threads(self.context, {})
        self.assertEqual([row["thread_id"] for row in result["threads"]], [100])
        self.assertEqual(result["omitted_inaccessible_count"], 1)
        self.assertEqual(self.context.state.source_channels, {100})

    async def test_scoped_ass_evidence_can_feed_generic_spreadsheet_output(self):
        result = await cwl_ass_scores(self.context, {
            "clan_code": "BE1", "war_ids": ["CWL:#WAR"],
        })
        self.context.guild.filesize_limit = 8 * 1024 * 1024
        player = result["players"][0]
        with patch(
            "elbow_helper.features.agent.files.spreadsheet_tools.render_workbook_bytes",
            AsyncMock(return_value=b"xlsx"),
        ):
            spreadsheet = await prepare_spreadsheet(self.context, {
                "title": "BE1 CWL war evidence",
                "sheets": [{
                    "name": "Observed inputs",
                    "columns": [
                        "Account", "Attacks", "Stars", "ASS status",
                    ],
                    "rows": [[
                        player["player_name"], str(player["attacks"]),
                        str(player["stars"]), result["scoring_status"],
                    ]],
                }],
            })
        self.assertEqual(spreadsheet["filename"], "be1-cwl-war-evidence.xlsx")
        self.assertEqual(spreadsheet["rows"], 1)
        self.assertEqual(len(self.context.state.attachments), 1)

    async def test_configured_bonus_scope_is_retained_and_distinct_from_ass(self):
        result = await cwl_bonus_scores(self.context, {
            "clan_code": "BE1", "war_ids": ["#WAR"],
        })
        self.assertEqual(result["metric_name"], "Configured CWL bonus adjusted delta")
        self.assertFalse(result["ass_distinction"]["is_ass"])
        self.assertEqual(result["attack_sample"][0]["adjusted_delta"], 1.0)
        self.assertEqual(result["settings"]["revision"], 4)

    async def test_bonus_scope_access_loss_before_retention_fails_closed(self):
        with patch(
            "elbow_helper.features.agent.capabilities.cwl.scoring.require_evidence_access",
            AsyncMock(side_effect=[None, AgentAccessLost("revoked")]),
        ):
            with self.assertRaises(AgentAccessLost):
                await cwl_bonus_scores(self.context, {
                    "clan_code": "BE1", "war_ids": ["#WAR"],
                })
        self.assertEqual(self.context.state.reports, {})

    async def test_report_is_scoped_to_current_guild(self):
        self.context.state.reports["foreign"] = CwlPerformanceReport("foreign", 2, _snapshot())
        result = await read_cwl_performance_report(self.context, {"report_id": "foreign"})
        self.assertIn("error", result)
