"""CWL scoring panel changes show old and new values before saving."""

import copy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.tools.cwl_bonus_scoring import (
    prepare_cwl_bonus_scoring, prepare_cwl_bonus_scoring_undo,
)


class CwlBonusScoringActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_score_change_and_undo_use_the_feature_revision(self):
        payload = {"matchup_expected": {"1:1": 2.0},
                   "uphit_bonus_per_level": 0.1, "downhit_penalty_per_level": 0.2,
                   "downhit_severe_after": 2, "downhit_severe_base": 0.3,
                   "downhit_severe_multiplier": 1.2}
        current = copy.deepcopy(payload)
        revision = 4

        def snapshot(clan):
            return copy.deepcopy(current), {}, revision

        def save(clan, next_payload, actor, *, expected_revision, summary):
            nonlocal current, revision
            self.assertEqual(expected_revision, revision)
            current = copy.deepcopy(next_payload)
            revision += 1
            return {"clans": {clan: copy.deepcopy(current)}}

        workflow = SimpleNamespace(
            bonus_scoring_snapshot=snapshot,
            bonus_scoring_issues=lambda clan, proposed: [],
            save_bonus_scoring=save,
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda name: workflow),
            member=SimpleNamespace(id=5), state=AgentTurnState(),
        )
        with patch("elbow_helper.features.agent.tools.cwl_bonus_scoring.require_evidence_access",
                   new_callable=AsyncMock):
            result = await prepare_cwl_bonus_scoring(context, {
                "clan_code": "X", "operation": "score",
                "attacker_th": 1, "defender_th": 1, "score": 2.5,
            })
        self.assertEqual(result["status"], "confirmation_required")
        action = context.state.command_proposals[0]
        self.assertTrue(any("2.0 to 2.5" in line for line in action.preview.lines))
        self.assertTrue(await action.preview.recheck())
        outcome = await action.run()
        self.assertEqual(current["matchup_expected"]["1:1"], 2.5)
        undo = await prepare_cwl_bonus_scoring_undo(context, {
            "targets": action.values, "before": action.preview.before,
            "after": outcome.after,
        })
        self.assertTrue(await undo.preview.recheck())
        await undo.run()
        self.assertEqual(current, payload)
