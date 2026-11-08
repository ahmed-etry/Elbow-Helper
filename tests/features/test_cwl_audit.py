"""League rules across storage, reports, boards, reminders and scoring; synthetic facts only."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.data.reads import WarReads
from elbow_helper.features.clan_reporting.record_wars import monthly_results
from elbow_helper.features.clan_health.analysis import ClanHealthAnalyzer
from elbow_helper.features.clan_health.record_wars import RegularWars
from elbow_helper.features.cwl.bonus.analysis import BonusAnalysisService
from elbow_helper.features.cwl.evidence import require_scoring_evidence
from elbow_helper.features.cwl.roster.analysis import (
    build_ass_season_metrics, profiles_for_roster_history,
)
from elbow_helper.features.cwl.router import CwlRouterMixin
from tests.data.test_war_reads import page
from tests.data.test_wars import group
from tests.features import test_cwl_record as fixtures

SECOND, DAY, NOV = fixtures.SECOND, fixtures.DAY, fixtures.NOV


class LeagueAuditRecordTests(fixtures.LeagueFixture, unittest.IsolatedAsyncioTestCase):


    async def test_dashboard_rejects_partial_final_attack_lists_and_warns_about_missing_rounds(self):
        at = NOV + DAY + SECOND
        await self.record(group(rounds=[{"warTags": ["#R0", "#R2"]}], season="2026-11"),
                          kind="cwl_group", at=at)
        await self.record(fixtures.league_war("warEnded", 1), kind="cwl_war", scope="#R0", at=at)
        await self.execute("DELETE FROM war_attacks WHERE attack_order=1")
        with fixtures.now(at):
            wars, warnings = await self.groups.current_wars("ONE")
        self.assertEqual(wars, [])
        self.assertIn("ONE: 1 CWL war couldn't be loaded.", warnings)

    async def test_unknown_final_round_result_keeps_the_monthly_post_pending(self):
        at = NOV + 30 * DAY
        await self.record(page(), kind="war_log", at=at)
        final = fixtures.league_war("warEnded", 1)
        final["opponent"].pop("stars")
        await self.record(final, kind="cwl_war", scope="#R0", at=at)
        result = await monthly_results(WarReads(self.store), "#PY", "2026-11", as_of=at)
        self.assertEqual(result["unknown_result"], 1)
        from elbow_helper.features.clan_reporting.wars import ClanReportingWarMixin
        manager = ClanReportingWarMixin()
        manager.war_reads = WarReads(self.store)
        with fixtures.now(at):
            status, _ = await manager._month_war_counts("#PY", datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertEqual(status, "waiting")


    async def test_health_ignores_future_preparation_and_reports_only_confirmed_league_misses(self):
        await self.november()
        reads = WarReads(self.store)
        league = RegularWars(reads, fixtures.CLANS, types=("CWL",))
        analyzer = ClanHealthAnalyzer(SimpleNamespace(), RegularWars(reads, fixtures.CLANS), league)
        start = datetime.fromtimestamp(NOV // SECOND, timezone.utc)
        end = datetime.fromtimestamp((fixtures.end_of(2) - 3600 * SECOND) // SECOND, timezone.utc)

        async def health():
            rows = [{"player_tag": "#P2", "clan_code": "ONE"}]
            await asyncio.to_thread(analyzer.apply_war_activity, rows, cycle_start=start, cycle_end=end)
            with patch("elbow_helper.features.clan_health.analysis.flags.effective_player_rules",
                       return_value={"_profile_name": "competitive", "war": {"missed_attack_rate_percent": 0}}):
                await asyncio.to_thread(analyzer.apply_flags, rows, cycle_start=start, cycle_end=end)
            return rows[0]

        with fixtures.now(int(end.timestamp()) * SECOND):
            row = await health()
            self.assertEqual((row["cwl_events_joined"], row["cwl_hits_expected"], row["cwl_hits_used"], row["cwl_missed"]),
                             (2, 2, 1, 0))
            self.assertEqual(row["war_missed"], 0)
            self.assertEqual(row["signal_cards"]["cwl_hit_usage"]["verdict"], "Good")
            counts = await asyncio.to_thread(league.war_counts, cycle_start_ts=int(start.timestamp()),
                                             cycle_end_ts=int(end.timestamp()))
            self.assertEqual(counts, {"ONE": 2})
        final = fixtures.league_war("inWar", 2)
        final["state"] = "warEnded"
        end = datetime.fromtimestamp((fixtures.end_of(2) + SECOND) // SECOND, timezone.utc)
        await self.record(final, kind="cwl_war", scope="#R8", at=int(end.timestamp()) * SECOND)
        with fixtures.now(int(end.timestamp()) * SECOND):
            row = await health()
            self.assertEqual((row["cwl_events_joined"], row["cwl_missed"], row["war_missed"]), (2, 1, 1))
            self.assertEqual(row["signal_cards"]["cwl_hit_usage"]["verdict"], "Needs Review")

    async def test_monthly_summary_waits_for_undrawn_rounds_after_the_month_has_closed(self):
        at = NOV + 30 * DAY
        await self.record(group(season="2026-11"), kind="cwl_group", at=at)
        await self.record(fixtures.league_war("warEnded", 1), kind="cwl_war", scope="#R0", at=at)
        await self.record(page(), kind="war_log", at=at)
        result = await monthly_results(WarReads(self.store), "#PY", "2026-11", as_of=at)
        self.assertEqual(result["coverage"]["league_rounds"]["pending_rounds"], [{"clan_tag": "#PY", "round": 2}])

    async def test_missing_pairings_count_once_per_round_and_preserve_the_monthly_retry(self):
        at = NOV + 30 * DAY
        await self.record(group(rounds=[{"warTags": ["#R0", "#R2"]}, {"warTags": ["#R8", "#R9"]}],
                                season="2026-11"), kind="cwl_group", at=at)
        await self.record(fixtures.league_war("warEnded", 1), kind="cwl_war", scope="#R0", at=at)
        await self.record(page(), kind="war_log", at=at)
        with fixtures.now(at):
            _, warnings = await self.groups.current_wars("ONE")
        self.assertEqual(warnings, ["ONE: 1 CWL war couldn't be loaded."])
        result = await monthly_results(WarReads(self.store), "#PY", "2026-11", as_of=at)
        self.assertEqual({row["round"] for row in result["coverage"]["league_rounds"]["missing_wars"]}, {2})


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 15, 12, tzinfo=timezone.utc)


class LeagueAuditPresentationTests(unittest.IsolatedAsyncioTestCase):
    def router(self, wars):
        router = CwlRouterMixin()
        router.state = {"last_poll_ts": 0}
        router._get_league_wars = AsyncMock(return_value=wars)
        router._save_state = AsyncMock()
        router.sync_registered_cwl_thread = AsyncMock()
        router._sync_cwl_channel = AsyncMock()
        router._log_missed_api = AsyncMock(return_value=True)
        return router

    async def poll(self, router):
        with patch("elbow_helper.features.cwl.router.CWL_CLAN_TAGS", {"ONE": "#PY"}), \
                patch("elbow_helper.features.cwl.router.datetime", FixedDatetime):
            await router._poll_once()

    async def test_final_reminders_retry_after_signup_days_and_do_not_penalize_benched_players(self):
        war = fixtures.league_war("inWar", 1)
        war.update(_state="warEnded", state="warEnded", _warTag="#R0", _round=1)
        router = self.router([war])
        router._log_missed_api.return_value = False
        await self.poll(router)
        self.assertEqual(router.state["last_poll_ts"], 0)
        router._log_missed_api.return_value = True
        await self.poll(router)
        self.assertTrue(router.state["missed_posted"]["ONE:#R0"])
        self.assertEqual(router._log_missed_api.await_args.args[2], ["Player #P2 (1 missed)"])
        self.assertTrue(router._log_missed_api.await_args.kwargs["check_existing"])

    async def test_unavailable_and_partial_rounds_do_not_advance_the_cursor_or_invent_misses(self):
        router = self.router(None)
        await self.poll(router)
        self.assertEqual(router.state["last_poll_ts"], 0)
        final = fixtures.league_war("warEnded", 1)
        final.update(_state="warEnded", _warTag="#R0", _round=1,
                     _coverage={"own": {"lineup_complete": True, "attacks_complete": False}})
        router._get_league_wars.return_value = [final]
        await self.poll(router)
        self.assertEqual(router.state["last_poll_ts"], 0)
        router._log_missed_api.assert_not_awaited()
        self.assertEqual(router.state["missed_posted"], {})

    async def test_unavailable_group_and_missing_pairings_are_distinct_from_no_league(self):
        router = self.router([])
        router.cwl_groups = SimpleNamespace(group=AsyncMock(return_value=None))
        self.assertIsNone(await CwlRouterMixin._get_league_wars(router, "ONE"))
        signal = {"status": "observed", "last_outcome": "not_in_war"}
        router.cwl_groups.group.return_value = {"signal": signal, "coverage": {}, "wars": []}
        self.assertEqual(await CwlRouterMixin._get_league_wars(router, "ONE"), [])
        router.cwl_groups.group.return_value["coverage"] = {"missing_wars": ["#R0"]}
        self.assertIsNone(await CwlRouterMixin._get_league_wars(router, "ONE"))


class LeagueAuditScoringTests(unittest.TestCase):
    def sample(self):
        war = {"war_id": "CWL:#R0", "clan_code": "ONE", "cwl_season": "2026-10",
               "cwl_league": "Crystal League I", "end_ts": 1, "team_size": 2}
        member = {"war_id": war["war_id"], "clan_code": "ONE", "player_tag": "#P0", "player_name": "Synthetic",
                  "townhall": 16, "map_position": 1, "attacks_expected": 1, "attacks_used": 1}
        attack = {"war_id": war["war_id"], "clan_code": "ONE", "player_tag": "#P0", "stars": 2,
                  "destruction": 80, "defender_map_position": 1, "defender_townhall": 16}
        return war, member, attack

    def test_unknown_counts_missing_attacks_and_missing_results_never_become_zero_scores(self):
        for failure in ("used", "expected", "attack", "stars", "destruction", "running"):
            with self.subTest(failure=failure):
                war, member, attack = self.sample()
                hits = [attack]
                if failure in ("used", "expected"):
                    member["attacks_" + failure] = None
                elif failure == "attack":
                    hits = []
                elif failure == "running":
                    war["state"] = "inWar"
                else:
                    attack[failure] = None
                with self.assertRaisesRegex(ValueError, "Stored war evidence is unavailable"):
                    require_scoring_evidence([war], [member], hits)
                war.update(roster=[member], attacks=hits)
                scored = BonusAnalysisService(None, None).analyze_wars("ONE", [war], {})
                self.assertEqual(scored[:3], ([], [], []))
                self.assertEqual(scored[4], ["ONE: CWL data couldn't be loaded."])

    def test_round_swaps_add_only_the_rounds_each_player_fought(self):
        war, member, attack = self.sample()
        later = {**war, "war_id": "CWL:#R8", "end_ts": 2}
        wars = [war, later]
        profiles, _ = profiles_for_roster_history(wars)
        metrics = build_ass_season_metrics(wars=wars,
            roster=[member, {**member, "war_id": later["war_id"], "player_tag": "#P2"}],
            attacks=[attack, {**attack, "war_id": later["war_id"], "player_tag": "#P2"}],
            season_order={"2026-10": 1}, profiles_by_clan=profiles)
        self.assertEqual([metric.attacks_expected for metric in metrics], [1, 1])
        self.assertEqual([metric.wars for metric in metrics], [1, 1])
