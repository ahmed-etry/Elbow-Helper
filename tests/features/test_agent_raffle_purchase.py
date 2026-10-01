"""Raffle hub purchases spend coins only after one confirmation."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.tools.raffle_purchase import prepare_raffle_purchase


class RafflePurchaseActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_purchase_previews_cost_and_balance_before_buying(self):
        state = {"month_key": 1, "cost": 100, "balance": 250,
                 "has_ticket": False, "issue": None}
        workflow = SimpleNamespace(
            raffle_purchase_state=AsyncMock(return_value=state),
            buy_raffle_ticket=AsyncMock(return_value=(True, "Ticket purchased for this month.")),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda name: workflow),
            member=SimpleNamespace(id=4, mention="<@4>"),
            state=AgentTurnState(),
        )
        with patch("elbow_helper.features.agent.tools.raffle_purchase.require_evidence_access",
                   new_callable=AsyncMock):
            result = await prepare_raffle_purchase(context, {})
        self.assertEqual(result["status"], "confirmation_required")
        action = context.state.command_proposals[0]
        self.assertIs(action.action_class, ActionClass.IRREVERSIBLE)
        self.assertTrue(any("250 to 150" in line for line in action.preview.lines))
        workflow.buy_raffle_ticket.assert_not_awaited()
        self.assertTrue(await action.preview.recheck())
        outcome = await action.run()
        self.assertEqual(outcome.text, "Ticket purchased for this month.")
        workflow.buy_raffle_ticket.assert_awaited_once_with(4)
