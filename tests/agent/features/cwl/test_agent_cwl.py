from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from unittest.mock import patch

from elbow_helper.configuration.roles import CORE, CO_APPLICANT_ROLE_ID
from elbow_helper.agent.safety.access import AgentAccessLost
from elbow_helper.agent.engine.models import AgentRequestContext
from elbow_helper.agent.features.cwl.reads import read_cwl_threads
from elbow_helper.agent.reads.cwl import cwl_bonus_scores
from elbow_helper.features.cwl.bonus.analysis import BonusAnalysisService
from elbow_helper.features.cwl.queries import CwlQueries
from elbow_helper.infrastructure.clash import ClashClient
from tests.features.test_cwl_queries import _History, _BonusConfig, _dataset

from elbow_helper.features.cwl.queries import CwlAssScopeRow, CwlAssWarsSnapshot, CwlBonusAttackScore, CwlBonusWarsSnapshot, CwlBonusSettings, CwlClanSeasonSummary, CwlPerformanceRow, CwlPerformanceSnapshot, CwlThreadRegistration


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
    result_snapshot = CwlQueries.result_snapshot
    results = CwlQueries.results

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


    def setUp(self):
        member = SimpleNamespace(id=42, display_name="Tester", roles=[SimpleNamespace(id=next(iter(CORE))), SimpleNamespace(id=CO_APPLICANT_ROLE_ID)])
        guild = SimpleNamespace(id=1, name="Brown Elbow", me=member, get_member=lambda _: member)
        channel = SimpleNamespace(id=100, guild=guild, permissions_for=lambda _: SimpleNamespace(
            view_channel=True, read_message_history=True,
        ))
        guild.get_channel_or_thread = lambda value: channel if value == 100 else None
        self.queries = _Queries()
        self.context = AgentRequestContext(private_pass=True,
            bot=SimpleNamespace(fetch_channel=AsyncMock(return_value=None)), guild=guild, member=member,
            source_message=SimpleNamespace(
                id=500, channel=channel, created_at=datetime.now(timezone.utc),
            ),
            account_links=None, message_search=None,
            cwl_queries=self.queries,
        )


    async def test_registered_threads_include_only_currently_accessible_sources(self):
        result = await read_cwl_threads(self.context, {})
        self.assertEqual([row["thread_id"] for row in result["threads"]], [100])
        self.assertEqual(result["omitted_inaccessible_count"], 1)
        self.assertEqual(self.context.state.source_channels, {100})


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
            "elbow_helper.agent.reads.cwl.require_evidence_access",
            AsyncMock(side_effect=[None, AgentAccessLost("revoked")]),
        ):
            with self.assertRaises(AgentAccessLost):
                await cwl_bonus_scores(self.context, {
                    "clan_code": "BE1", "war_ids": ["#WAR"],
                })
        self.assertEqual(self.context.state.reports, {})
