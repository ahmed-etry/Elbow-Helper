"""Command outcomes cannot replace an independently requested read answer."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.actions.outcomes import ActionOutcome
from tests.agent.safety import test_evidence_scope as scopes
from elbow_helper.agent.safety.access import AgentAccessLost


class MixedDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        scopes.ScopeTests.setUp(self)

    async def test_command_outcome_does_not_bypass_read_audience_check(self):
        self.context.state.outcomes.append(ActionOutcome("complete", text="Command result"))
        self.context.state.has_read_answer = True
        from elbow_helper.agent.conversation.delivery import AgentDeliveryMixin
        delivery = AgentDeliveryMixin()
        delivery.bot = SimpleNamespace(user=self.bot_member)
        with patch("elbow_helper.agent.conversation.delivery.assert_answer_scope",
                   new=AsyncMock(side_effect=AgentAccessLost("Out-of-scope answer citation"))) as guard:
            with self.assertRaises(AgentAccessLost):
                await delivery.send_response(self.message, "Secret answer\n\nCommand result", None,
                                             context=self.context)
        guard.assert_awaited_once()
        self.message.reply.assert_not_awaited()
