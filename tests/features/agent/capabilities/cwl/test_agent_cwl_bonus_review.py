"""CWL bonus review decisions share their feature's result path."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.capabilities.cwl.bonus_review import prepare_cwl_bonus_review


class CwlBonusReviewActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_hold_decision_waits_for_confirmation(self):
        board = {"board_key": "review:22", "closed": False,
                 "month_label": "October 2026", "clan": {"status": "needs_review"}}
        workflow = SimpleNamespace(
            bonus_current_month_key=lambda: 22,
            bonus_review_state=lambda clan, *, mode, month_key: board,
            set_bonus_review_status=AsyncMock(return_value="BEH put on hold for now."),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda name: workflow),
            member=SimpleNamespace(id=5), state=AgentTurnState(),
        )
        with patch("elbow_helper.features.agent.capabilities.cwl.bonus_review.require_evidence_access",
                   new_callable=AsyncMock):
            result = await prepare_cwl_bonus_review(
                context, {"clan_code": "BEH", "decision": "hold"})
        self.assertEqual(result["status"], "confirmation_required")
        action = context.state.command_proposals[0]
        self.assertTrue(await action.preview.recheck())
        workflow.set_bonus_review_status.assert_not_awaited()
        outcome = await action.run()
        self.assertEqual(outcome.text, "BEH put on hold for now.")
        workflow.set_bonus_review_status.assert_awaited_once_with(
            "review:22", "BEH", "on_hold", context.member)
