from datetime import datetime, timezone

import unittest

from elbow_helper.features.cwl.config import CWL_CLAN_NAMES
from elbow_helper.features.cwl.bonus.analysis import BonusAnalysisService
from elbow_helper.features.cwl.queries import CwlQueries
from elbow_helper.infrastructure.clash import ClashClient


class _History:
    def __init__(self, dataset):
        self.dataset = dataset
        self.limits = []
        self.scopes = []

    def roster_history(self, history_limit, *, season=None, clan_code=None):
        self.limits.append(history_limit)
        self.scopes.append((season, clan_code))
        return self.dataset

    def bonus_seasons(self, clan_codes=None):
        codes = set(clan_codes or ())
        return sorted({
            str(war["cwl_season"]) for war in self.dataset["wars"]
            if not codes or str(war["clan_code"]) in codes
        }, reverse=True)


    def scoring_wars(self, clan_code, war_ids):
        selected = [war for season in self.bonus_seasons([clan_code])
                    for war in self.bonus_wars(clan_code, season)
                    if war["war_id"].removeprefix("CWL:") in war_ids]
        missing = set(war_ids) - {war["war_id"].removeprefix("CWL:") for war in selected}
        if missing:
            raise ValueError("Unknown CWL war IDs: " + ", ".join(sorted(missing)))
        return selected

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
    def test_performance_accepts_a_catchup_league_key(self):
        dataset = _dataset()
        for war in dataset["wars"]:
            war["cwl_season"] = "2026-08-catchup"
        dataset["seasons"][0]["key"] = "2026-08-catchup"
        result = CwlQueries(_History(dataset), lambda: {}).performance(season="2026-08-catchup")
        self.assertEqual(result.rows[0].season, "2026-08-catchup")


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

    def test_full_season_war_set_matches_feature_season_scores(self):
        queries = self.bonus_queries()
        ids = [f"war-{number}" for number in range(1, 8)]
        ass = queries.ass_wars(clan_code="BEH", war_ids=ids)
        feature = queries.performance(season="2026-08", clan_code="BEH")
        self.assertEqual(ass.rows[0].ass_score, feature.rows[0].score)
        self.assertEqual(ass.rows[0].ass_score, 23.8)
        self.assertEqual(ass.completed_wars, 7)
        self.assertEqual(len(ass.attack_sample), 7)
        bonus = queries.bonus_wars(clan_code="BEH", war_ids=ids)
        summary, _, attacks, _, _ = queries._bonus_analysis.analyze_clan(
            "BEH", "2026-08", queries._bonus_config.config,
        )
        self.assertEqual(bonus.summaries[0].total_adjusted_delta,
                         summary[0]["total_adjusted_delta"])
        self.assertEqual(len(bonus.attacks), len(attacks))

    def test_one_war_projects_to_seven_attacks_with_exact_sample(self):
        queries = self.bonus_queries()
        result = queries.ass_wars(clan_code="BEH", war_ids=["war-3"])
        self.assertEqual(result.resolved_rounds, (3,))
        self.assertEqual(result.resolved_war_ids, ("war-3",))
        self.assertEqual(result.rows[0].attacks, 1)
        self.assertEqual(result.rows[0].projected_stars, 21.0)
        self.assertEqual(result.attack_sample[0]["war_id"], "war-3")
        bonus = queries.bonus_wars(clan_code="BEH", war_ids=["war-3"])
        self.assertEqual(bonus.summaries[0].attack_count, 1)
        self.assertEqual(bonus.attacks[0].adjusted_delta, 1.0)

    def test_chosen_rounds_are_scored_as_one_combined_sample(self):
        dataset = _dataset()
        for index, stars in ((1, 1), (3, 1), (4, 3)):
            dataset["attacks"][index].update(stars=stars, destruction=50)
        queries = self.bonus_queries(dataset)
        ids = ["war-2", "war-4", "war-5"]
        combined = queries.ass_wars(clan_code="BEH", war_ids=ids)
        self.assertEqual(combined.resolved_rounds, (2, 4, 5))
        self.assertEqual(combined.rows[0].attacks, 3)
        self.assertEqual(combined.rows[0].stars, 5)
        self.assertAlmostEqual(combined.rows[0].projected_stars, 35 / 3)
        self.assertEqual(len(combined.attack_sample), 3)
        bonus = queries.bonus_wars(clan_code="BEH", war_ids=ids)
        self.assertEqual(bonus.summaries[0].attack_count, 3)
        for key in ids:
            single = queries.ass_wars(clan_code="BEH", war_ids=[key])
            self.assertNotEqual(combined.rows[0].ass_score, single.rows[0].ass_score)
            one_bonus = queries.bonus_wars(clan_code="BEH", war_ids=[key])
            self.assertNotEqual(bonus.summaries[0].average_adjusted_delta,
                                one_bonus.summaries[0].average_adjusted_delta)

    def test_wars_from_two_seasons_produce_one_player_result(self):
        dataset = _dataset()
        dataset["wars"][0]["cwl_season"] = "2026-07"
        dataset["attacks"][0].update(stars=1, destruction=50)
        queries = self.bonus_queries(dataset)
        ids = ["war-1", "war-2"]
        result = queries.ass_wars(clan_code="BEH", war_ids=ids)
        self.assertEqual(result.seasons, ("2026-07", "2026-08"))
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0].projected_stars, 14.0)
        self.assertEqual(result.rows[0].attacks, 2)
        bonus = queries.bonus_wars(clan_code="BEH", war_ids=ids)
        self.assertEqual(bonus.seasons, result.seasons)
        self.assertEqual(len(bonus.summaries), 1)
        self.assertEqual(bonus.summaries[0].attack_count, 2)

    def test_chosen_war_ids_accept_prefix_and_reject_duplicates_and_caps(self):
        queries = self.bonus_queries()
        result = queries.ass_wars(clan_code="BEH", war_ids=["CWL:war-1"])
        self.assertEqual(result.resolved_war_ids, ("war-1",))
        for ids in ([], ["war-1", "CWL:war-1"], [""], list(map(str, range(57)))):
            for method in (queries.ass_wars, queries.bonus_wars):
                with self.subTest(ids=ids, method=method), self.assertRaises(ValueError):
                    method(clan_code="BEH", war_ids=ids)

    def test_bonus_chosen_wars_report_configuration_failures(self):
        queries = self.bonus_queries()
        queries._bonus_config.config = None
        with self.assertRaisesRegex(ValueError, "settings are unavailable"):
            queries.bonus_wars(clan_code="BEH", war_ids=["war-1"])
