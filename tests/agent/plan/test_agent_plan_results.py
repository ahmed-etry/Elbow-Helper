"""Model-facing lookup results keep data and bounded flags."""

import unittest
from dataclasses import asdict

from elbow_helper.agent.plan.results import model_result, plan_feedback
from elbow_helper.features.cwl.queries import CwlPerformanceRow


class ModelResultTests(unittest.TestCase):


    def test_result_status_and_repeated_projection_preserve_evidence(self):
        rows = [{'note': 'authored source text', 'value': 0}]
        cases = (
            ({'complete': True}, 'complete'),
            ({'error': 'That source is not accessible.',
              'limitations': ['The selected page is incomplete.']}, 'failed'),
            ({'search_is_exhaustive': False}, 'partial'),
            ({'search_complete': False}, 'partial'),
            ({'coverage_complete': False}, 'partial'),
            ({'complete_snapshot': True, 'next_offset': 7}, 'partial'),
            ({'complete_snapshot': True, 'next_cursor': 'synthetic'}, 'partial'),
            ({'flags': {'status': 'partial', 'truncated': True,
                        'limits': ['synthetic_limit'],
                        'coverage_dates': {'after': '2026-01-01'}}}, 'partial'),
        )
        for fields, status in cases:
            with self.subTest(fields=fields):
                data = {'rows': rows, **fields}
                result = model_result(data)
                self.assertEqual(result['flags']['status'], status)
                self.assertEqual(result['rows'], rows)
                for key, value in fields.items():
                    self.assertEqual(result[key], value)
                self.assertEqual(model_result(result), result)


    def test_plan_feedback_preserves_rule_and_offered_values_as_data(self):
        result = plan_feedback("Use the registered value.", step_id="second", offered=["202"])
        self.assertEqual(result["flags"], {"status": "refused", "rule": "use_the_registered_value"})
        self.assertEqual(result["step_id"], "second")
        self.assertEqual(result["offered"], ["202"])
        self.assertEqual(result["error"], "Use the registered value.")


def synthetic_cwl_payload():
    return {"players": [asdict(CwlPerformanceRow(
        season="2026-01", clan_code="SYN", league="Synthetic League",
        profile_key="synthetic_profile", player_tag=f"#SYN{index}",
        player_name=f"Synthetic player {index}", townhall=18, wars=7, attacks=7,
        attacks_expected=7, stars=21, average_destruction=100.0, score=21.0,
        rank=index + 1, rank_total=400, multi_season_score=21.0,
        multi_season_rank=index + 1, multi_season_rank_total=400,
    )) for index in range(400)]}
