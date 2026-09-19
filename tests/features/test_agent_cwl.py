from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from unittest.mock import patch

from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.reports.cwl import CwlPerformanceReport
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.features.agent.reports.roles import RoleAccountReport
from elbow_helper.features.agent.tools.cwl import read_cwl_performance, read_cwl_performance_report, read_cwl_threads
from elbow_helper.features.agent.tools.cwl_scoring import (
    list_cwl_ass_seasons, read_cwl_ass_scope, read_cwl_ass_scope_report,
    read_cwl_bonus_scope, read_cwl_bonus_scope_report,
)
from elbow_helper.features.agent.tools.spreadsheets import prepare_spreadsheet
from elbow_helper.features.account_links.evidence import (
    AccountTagEvidence, AccountTagEvidenceSnapshot,
)
from elbow_helper.features.cwl.queries import (
    CwlAssScopeRow, CwlAssScopeSnapshot, CwlBonusAttackScore,
    CwlBonusScopeSnapshot, CwlBonusSettings, CwlClanSeasonSummary,
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
        self.threads = (
            CwlThreadRegistration("BEH", "Brown Elbow Heroes", "#P0", 100, "now"),
            CwlThreadRegistration("BEC", "Brown Elbow Clan", "#P2", 200, None),
        )

    def performance(self, *, history_limit):
        self.calls.append(history_limit)
        return self.snapshot

    def registered_threads(self):
        return self.threads

    def ass_seasons(self, *, clan_code):
        self.calls.append(("ass_seasons", clan_code))
        return ("2026-08",)

    def ass_scope(
        self, *, clan_code, season, scope_type, cwl_round=None, war_id=None,
    ):
        self.calls.append((
            "ass_scope", clan_code, season, scope_type, cwl_round, war_id,
        ))
        return CwlAssScopeSnapshot(
            "2026-09-17T12:00:00+00:00", clan_code, season, scope_type,
            cwl_round, war_id, (war_id or "war-1",),
            (cwl_round or 1,), 1, "Champion League II", "high_2026_06",
            "High League Standard (Jun 2026)", 0.4, "high_linear",
            (
                "calculated_from_selected_scope"
            ),
            (
                "selected_season_completed_wars"
                if scope_type == "season" else "selected_completed_wars"
            ),
            (CwlAssScopeRow(
                "#P0", "Alpha", 18, 1, 1, 1, 3, 100.0,
                1.0, 0.0, 1.0, 21.0, 1, 1,
                21.0, 0.0, 0.0, 0.0,
            ),),
        )

    def bonus_scope(
        self, *, clan_code, season, scope_type, cwl_round=None, war_tag=None,
    ):
        self.calls.append((
            "bonus_scope", clan_code, season, scope_type, cwl_round, war_tag,
        ))
        return CwlBonusScopeSnapshot(
            "2026-09-17T12:00:00+00:00", clan_code, season, scope_type,
            cwl_round, war_tag, (cwl_round or 1,), (war_tag or "#WAR",),
            CwlBonusSettings(4, "2026-08-01T00:00:00+00:00", 2, 8,
                             0.15, 0.10, 0, 0.20, 2.0),
            (), (), (CwlBonusAttackScore(
                cwl_round or 1, war_tag or "#WAR", "#P0", "Alpha", 18,
                "#D", 18, 3, 100.0, 3.0, 2.0, 0, "18:18", 1.0,
                0.0, 1.0, 3, "",
            ),), (), "selected_scored_attacks",
        )


class AgentCwlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        member = SimpleNamespace(id=42, display_name="Tester", roles=[SimpleNamespace(id=next(iter(CORE)))])
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
            account_links=None, clan_health=None, message_search=None,
            cwl_queries=self.queries,
        )

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
        self.context.state.reports["wrong"] = RoleAccountReport("wrong", "now", (), ())
        self.assertIn("error", await read_cwl_performance_report(
            self.context, {"report_id": "wrong"},
        ))

    async def test_registered_threads_include_only_currently_accessible_sources(self):
        result = await read_cwl_threads(self.context, {})
        self.assertEqual([row["thread_id"] for row in result["threads"]], [100])
        self.assertEqual(result["omitted_inaccessible_count"], 1)
        self.assertEqual(self.context.state.source_channels, {100})

    async def test_scoped_ass_tools_preserve_projection_scope_and_sample(self):
        seasons = await list_cwl_ass_seasons(
            self.context, {"clan_code": "BEH"},
        )
        season = await read_cwl_ass_scope(self.context, {
            "clan_code": "BEH", "season": "2026-08",
            "scope_type": "season",
        })
        round_result = await read_cwl_ass_scope(self.context, {
            "clan_code": "BEH", "season": "2026-08",
            "scope_type": "round", "cwl_round": 1,
        })

        self.assertEqual(seasons["seasons"], ["2026-08"])
        self.assertEqual(
            season["scoring_status"], "calculated_from_selected_scope",
        )
        self.assertEqual(season["players"][0]["ass_score"], 21.0)
        self.assertEqual(
            round_result["scoring_status"], "calculated_from_selected_scope",
        )
        self.assertEqual(round_result["players"][0]["ass_score"], 21.0)
        self.assertEqual(round_result["players"][0]["attacks"], 1)
        self.assertEqual(round_result["projection_attack_target"], 7)
        self.assertIn("selected scope", round_result["projection_note"])
        self.assertIn("not automatically a completed-season", round_result["projection_note"])
        retained = await read_cwl_ass_scope_report(self.context, {
            "report_id": round_result["report_id"],
        })
        self.assertEqual(retained, round_result)

    async def test_scoped_ass_evidence_can_feed_generic_spreadsheet_output(self):
        result = await read_cwl_ass_scope(self.context, {
            "clan_code": "BE1", "season": "2026-08",
            "scope_type": "war", "war_id": "CWL:#WAR",
        })
        self.context.guild.filesize_limit = 8 * 1024 * 1024
        player = result["players"][0]
        with patch(
            "elbow_helper.features.agent.tools.spreadsheets.render_workbook_bytes",
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
        result = await read_cwl_bonus_scope(self.context, {
            "clan_code": "BE1", "season": "2026-08",
            "scope_type": "war", "war_tag": "#WAR",
        })

        self.assertEqual(result["metric_name"], "Configured CWL bonus adjusted delta")
        self.assertIn("not ASS", result["ass_distinction"])
        self.assertEqual(result["rows"][0]["adjusted_delta"], 1.0)
        self.assertEqual(result["settings"]["revision"], 4)
        retained = await read_cwl_bonus_scope_report(self.context, {
            "report_id": result["report_id"],
        })
        self.assertEqual(retained, result)

    async def test_bonus_scope_access_loss_before_retention_fails_closed(self):
        with patch(
            "elbow_helper.features.agent.tools.cwl_scoring.require_evidence_access",
            AsyncMock(side_effect=[None, AgentAccessLost("revoked")]),
        ):
            with self.assertRaises(AgentAccessLost):
                await read_cwl_bonus_scope(self.context, {
                    "clan_code": "BE1", "season": "2026-08",
                    "scope_type": "war", "war_tag": "#WAR",
                })

        self.assertEqual(self.context.state.reports, {})

    async def test_report_is_scoped_to_current_guild(self):
        self.context.state.reports["foreign"] = CwlPerformanceReport("foreign", 2, _snapshot())
        result = await read_cwl_performance_report(self.context, {"report_id": "foreign"})
        self.assertIn("error", result)
