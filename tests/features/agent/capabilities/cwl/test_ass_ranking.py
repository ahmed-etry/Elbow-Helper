"""Scorer ranking projections remain complete without deciding request intent."""

from copy import deepcopy
import json
import unittest

from elbow_helper.features.agent.capabilities.cwl.scoring import ass_ranking
from elbow_helper.features.agent.engine.budgets import MAX_TOOL_RESULT_CHARACTERS
from elbow_helper.features.agent.engine.tool_call import bound_tool_result
from elbow_helper.features.agent.plan.results import model_view


class AssRankingTests(unittest.TestCase):
    def test_feature_ranks_ties_unscored_accounts_and_negative_scores_are_preserved(self):
        players = [
            {"rank": 2, "player_name": "Second", "player_tag": "#P2", "ass_score": 90.125},
            {"rank": None, "player_name": "Unscored", "player_tag": "#P4", "ass_score": None},
            {"rank": 1, "player_name": "First", "player_tag": "#P1", "ass_score": -1.5},
            {"rank": 2, "player_name": "Tied", "player_tag": "#P3", "ass_score": 85.0},
        ]
        original = deepcopy(players)
        ranking = ass_ranking(players)
        self.assertTrue(ranking["complete"])
        self.assertEqual(ranking["account_count"], 4)
        self.assertEqual(ranking["text"].splitlines()[1:], [
            "1\tFirst\t#P1\t-1.50", "2\tSecond\t#P2\t90.12",
            "2\tTied\t#P3\t85.00", "-\tUnscored\t#P4\t-",
        ])
        self.assertEqual(players, original)

    def test_large_details_do_not_truncate_the_complete_ranking(self):
        players = [{"rank": index + 1, "player_name": f"Account {index}",
                    "player_tag": f"#P{index}", "ass_score": 21.0} for index in range(100)]
        ranking = ass_ranking(players)
        payload = {"players": players, "ranking": ranking,
                   "attack_sample": [{"war_id": f"war-{index}", "stars": 3, "description": "x" * 100}
                                     for index in range(4000)]}
        content = json.dumps(model_view(payload), ensure_ascii=False, separators=(",", ":"))
        bounded = bound_tool_result(content, MAX_TOOL_RESULT_CHARACTERS)
        result = json.loads(bounded)
        self.assertLessEqual(len(bounded), MAX_TOOL_RESULT_CHARACTERS)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["ranking"], ranking)
        self.assertEqual(len(result["ranking"]["text"].splitlines()), 101)

    def test_ranking_too_large_for_budget_is_omitted_instead_of_falsely_complete(self):
        ranking = ass_ranking([{"rank": 1, "player_name": "x" * 500,
                                "player_tag": "#P0", "ass_score": 21.0}])
        result = json.loads(bound_tool_result(json.dumps({"ranking": ranking}), 64))
        self.assertNotIn("ranking", result)
        self.assertTrue(result["truncated"])

    def test_empty_selected_set_has_no_invented_accounts(self):
        ranking = ass_ranking([])
        self.assertEqual(ranking["account_count"], 0)
        self.assertEqual(len(ranking["text"].splitlines()), 1)
        self.assertTrue(ranking["complete"])
