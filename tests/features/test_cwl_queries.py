from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.cwl.config import CWL_CLAN_NAMES
from elbow_helper.features.cwl.bonus.analysis import BonusAnalysisService
from elbow_helper.features.cwl.queries import CwlQueries
from elbow_helper.features.cwl.roster.analysis import (
    build_ass_season_metrics, profiles_for_roster_history,
)
from elbow_helper.infrastructure.clash import ClashClient


class _History:
    def __init__(self, dataset):
        self.dataset = dataset
        self.limits = []

    def roster_history(self, history_limit):
        self.limits.append(history_limit)
        return self.dataset

    def bonus_seasons(self, clan_codes=None):
        codes = set(clan_codes or ())
        return sorted({
            str(war["cwl_season"]) for war in self.dataset["wars"]
            if not codes or str(war["clan_code"]) in codes
        }, reverse=True)

    def bonus_wars(self, clan_code, season):
        result = []
        for war in self.dataset["wars"]:
            if (
                war["clan_code"] != clan_code
                or war["cwl_season"] != season
            ):
                continue
            war_id = war["war_id"]
            result.append({
                **war,
                "roster": [
                    row for row in self.dataset["roster"]
                    if row["war_id"] == war_id
                    and row["clan_code"] == clan_code
                ],
                "attacks": [
                    row for row in self.dataset["attacks"]
                    if row["war_id"] == war_id
                    and row["clan_code"] == clan_code
                ],
            })
        return result


class _BonusConfig:
    def __init__(self, config=None, errors=()):
        self.config = config or {
            "revision": 4,
            "clan_meta": {"BEH": {"updated_at_utc": "2026-08-01T00:00:00+00:00"}},
            "clans": {"BEH": {
                "matchup_expected": {"18:18": 2.0},
                "max_downhit": 2, "max_uphit": 8,
                "downhit_penalty_per_level": 0.15,
                "uphit_bonus_per_level": 0.10,
                "downhit_severe_after": 0,
                "downhit_severe_base": 0.20,
                "downhit_severe_multiplier": 2.0,
            }},
        }
        self.errors = list(errors)

    def load(self):
        return self.config, self.errors


def _dataset():
    wars = []
    roster = []
    attacks = []
    for round_number in range(1, 8):
        war_id = f"war-{round_number}"
        wars.append({
            "war_id": war_id, "cwl_season": "2026-08", "clan_code": "BEH",
            "cwl_league": "Champion League II", "end_ts": 1000 + round_number,
            "cwl_round": round_number, "team_size": 15,
            "attacks_per_member": 1,
        })
        roster.append({
            "war_id": war_id, "clan_code": "BEH", "player_tag": "#AAA",
            "player_name": "Alpha", "townhall": 18, "map_position": 1,
            "attacks_expected": 1,
        })
        attacks.append({
            "war_id": war_id, "clan_code": "BEH", "player_tag": "#AAA",
            "stars": 3, "destruction": 100, "defender_map_position": 1,
            "defender_tag": f"#D{round_number}",
            "defender_townhall": 18, "attack_order": round_number,
        })
    return {
        "seasons": [{"key": "2026-08", "latest_end_ts": 1007}],
        "wars": wars, "roster": roster, "attacks": attacks,
    }


class CwlQueriesTests(unittest.TestCase):
    def bonus_queries(self, dataset=None, *, config=None):
        history = _History(dataset or _dataset())
        return CwlQueries(
            history, lambda: {},
            bonus_analysis=BonusAnalysisService(ClashClient(None), history),
            bonus_config=config or _BonusConfig(),
            clock=lambda: datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
        )

    def test_performance_returns_complete_typed_snapshot(self):
        history = _History(_dataset())
        queries = CwlQueries(
            history, lambda: {},
            clock=lambda: datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
        )

        snapshot = queries.performance(history_limit=3)

        self.assertEqual(history.limits, [3])
        self.assertEqual(snapshot.observed_at, "2026-09-17T12:00:00+00:00")
        self.assertEqual(snapshot.seasons, ("2026-08",))
        self.assertTrue(snapshot.clan_seasons[0].complete)
        self.assertEqual(snapshot.clan_seasons[0].wars, 7)
        self.assertEqual(snapshot.clan_seasons[0].attacks, 7)
        row = snapshot.rows[0]
        self.assertEqual((row.player_tag, row.attacks, row.attacks_expected), ("#AAA", 7, 7))
        self.assertEqual(row.rank, 1)
        self.assertEqual(row.multi_season_rank, 1)

    def test_history_limit_is_bounded_before_repository_read(self):
        history = _History(_dataset())
        queries = CwlQueries(history, lambda: {})
        for value in (0, 13, True, "3"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                queries.performance(history_limit=value)
        self.assertEqual(history.limits, [])

    def test_registered_threads_are_normalized_and_follow_live_state(self):
        clan_name = CWL_CLAN_NAMES["BEH"]
        state = {
            "123": {"clan_name": clan_name, "last_activity": "2026-09-17T10:00:00+00:00"},
            "bad": {"clan_name": clan_name},
            "456": {"clan_name": "Unknown"},
            "789": "invalid",
        }
        queries = CwlQueries(_History(_dataset()), lambda: state)

        first = queries.registered_threads()
        self.assertEqual(len(first), 1)
        self.assertEqual((first[0].clan_code, first[0].thread_id), ("BEH", 123))

        state.clear()
        self.assertEqual(queries.registered_threads(), ())

    def test_ass_scope_scores_the_selected_season_wars(self):
        queries = CwlQueries(
            _History(_dataset()), lambda: {},
            clock=lambda: datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
        )

        result = queries.ass_scope(
            clan_code="BEH", season="2026-08", scope_type="season",
        )

        self.assertEqual(result.scoring_status, "calculated_from_selected_scope")
        self.assertEqual(
            result.coverage_status, "selected_season_completed_wars",
        )
        self.assertEqual(result.completed_wars, 7)
        self.assertEqual(result.profile_key, "high_2026_06")
        self.assertEqual(result.rows[0].player_tag, "#AAA")
        self.assertEqual(result.rows[0].ass_score, 23.8)
        self.assertEqual(result.rows[0].rank, 1)

    def test_ass_scope_calculates_round_and_war_projections(self):
        queries = CwlQueries(_History(_dataset()), lambda: {})

        round_result = queries.ass_scope(
            clan_code="BEH", season="2026-08", scope_type="round",
            cwl_round=3,
        )
        war_result = queries.ass_scope(
            clan_code="BEH", season="2026-08", scope_type="war",
            war_id="war-4",
        )

        self.assertEqual(round_result.resolved_war_ids, ("war-3",))
        self.assertEqual(
            round_result.scoring_status, "calculated_from_selected_scope",
        )
        self.assertEqual(round_result.rows[0].attacks, 1)
        self.assertEqual(round_result.rows[0].projected_stars, 21.0)
        self.assertEqual(round_result.rows[0].ass_score, 23.8)
        self.assertEqual(war_result.resolved_rounds, (4,))
        self.assertEqual(
            war_result.scoring_status, "calculated_from_selected_scope",
        )
        self.assertEqual(war_result.rows[0].projected_stars, 21.0)

    def test_ass_scope_scores_partial_season_and_reports_missing_evidence(self):
        dataset = _dataset()
        dataset["wars"] = dataset["wars"][:3]
        dataset["roster"] = dataset["roster"][:3]
        dataset["attacks"] = dataset["attacks"][:3]
        queries = CwlQueries(_History(dataset), lambda: {})

        partial = queries.ass_scope(
            clan_code="BEH", season="2026-08", scope_type="season",
        )
        missing = queries.ass_scope(
            clan_code="BEH", season="2026-08", scope_type="war",
            war_id="unknown",
        )

        self.assertEqual(
            partial.scoring_status, "calculated_from_selected_scope",
        )
        self.assertEqual(
            partial.coverage_status, "selected_season_completed_wars",
        )
        self.assertTrue(all(row.ass_score is not None for row in partial.rows))
        self.assertEqual(missing.scoring_status, "unavailable")
        self.assertEqual(missing.coverage_status, "no_matching_completed_wars")
        self.assertEqual(missing.rows, ())
        for arguments in (
            {"clan_code": "BEH", "season": "2026-08", "scope_type": "round"},
            {"clan_code": "BEH", "season": "2026-08", "scope_type": "season", "cwl_round": 1},
            {"clan_code": "BAD", "season": "2026-08", "scope_type": "season"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                queries.ass_scope(**arguments)

    def test_partial_scope_scores_match_authoritative_ass_calculation(self):
        dataset = _dataset()
        dataset["attacks"][2].update({
            "stars": 2, "destruction": 75,
            "defender_map_position": 4,
        })
        history = _History(dataset)
        queries = CwlQueries(history, lambda: {})
        selected = history.bonus_wars("BEH", "2026-08")[2:3]
        wars = [{
            key: value for key, value in selected[0].items()
            if key not in {"roster", "attacks"}
        }]
        roster = [{
            **row, "war_id": wars[0]["war_id"], "clan_code": "BEH",
        } for row in selected[0]["roster"]]
        attacks = [{
            **row, "war_id": wars[0]["war_id"], "clan_code": "BEH",
        } for row in selected[0]["attacks"]]
        profiles, _ = profiles_for_roster_history(wars)
        expected = build_ass_season_metrics(
            wars=wars, roster=roster, attacks=attacks,
            season_order={"2026-08": 1}, profiles_by_clan=profiles,
        )[0]

        actual = queries.ass_scope(
            clan_code="BEH", season="2026-08", scope_type="round",
            cwl_round=3,
        ).rows[0]
        exact_war = queries.ass_scope(
            clan_code="BEH", season="2026-08", scope_type="war",
            war_id="war-3",
        ).rows[0]

        fields = {
            "ass_score": "score", "projected_stars": "projected_stars",
            "missed_stars": "missed_stars",
            "missed_adjustment": "missed_adjustment",
            "difficulty_adjustment": "difficulty_adjustment",
            "average_destruction": "average_destruction",
            "rank": "rank", "rank_total": "rank_total",
        }
        for actual_field, expected_field in fields.items():
            with self.subTest(field=actual_field):
                self.assertEqual(
                    getattr(actual, actual_field),
                    getattr(expected, expected_field),
                )
                self.assertEqual(
                    getattr(exact_war, actual_field),
                    getattr(expected, expected_field),
                )

    def test_ass_scope_resolves_a_clash_war_tag_to_stored_war_identity(self):
        dataset = _dataset()
        dataset["wars"][0]["war_id"] = "CWL:#WAR"
        dataset["roster"][0]["war_id"] = "CWL:#WAR"
        dataset["attacks"][0]["war_id"] = "CWL:#WAR"
        queries = CwlQueries(_History(dataset), lambda: {})

        result = queries.ass_scope(
            clan_code="BEH", season="2026-08", scope_type="war",
            war_id="#WAR",
        )

        self.assertEqual(result.requested_war_id, "#WAR")
        self.assertEqual(result.resolved_war_ids, ("CWL:#WAR",))

    def test_bonus_scope_reuses_configured_scoring_and_filters_after_analysis(self):
        queries = self.bonus_queries()

        season = queries.bonus_scope(
            clan_code="BEH", season="2026-08", scope_type="season",
        )
        round_result = queries.bonus_scope(
            clan_code="BEH", season="2026-08", scope_type="round",
            cwl_round=3,
        )
        war_result = queries.bonus_scope(
            clan_code="BEH", season="2026-08", scope_type="war",
            war_tag="war-4",
        )

        self.assertEqual(season.settings.revision, 4)
        self.assertEqual(season.settings.max_downhit, 2)
        self.assertEqual(season.coverage_status, "stored_season_scoring")
        self.assertEqual(len(season.summaries), 1)
        self.assertEqual(season.summaries[0].average_adjusted_delta, 1.0)
        self.assertEqual(len(season.attacks), 7)
        self.assertEqual(round_result.resolved_rounds, (3,))
        self.assertEqual(len(round_result.attacks), 1)
        self.assertEqual(round_result.attacks[0].adjusted_delta, 1.0)
        self.assertEqual(round_result.summaries, ())
        self.assertEqual(war_result.resolved_war_tags, ("war-4",))

    def test_bonus_scope_reports_missing_match_and_configuration_failures(self):
        queries = self.bonus_queries()
        missing = queries.bonus_scope(
            clan_code="BEH", season="2026-08", scope_type="war",
            war_tag="#NONE",
        )
        self.assertEqual(missing.coverage_status, "no_matching_scored_attacks")
        self.assertEqual(missing.attacks, ())

        unavailable = self.bonus_queries(config=_BonusConfig(config={}, errors=[
            "missing settings",
        ]))
        unavailable._bonus_config.config = None
        with self.assertRaisesRegex(ValueError, "settings are unavailable"):
            unavailable.bonus_scope(
                clan_code="BEH", season="2026-08", scope_type="season",
            )

