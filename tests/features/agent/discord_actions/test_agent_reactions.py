import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.discord_actions.reactions import react_to_request
from elbow_helper.features.agent.models import AgentTurnState


class ReactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_reaction_cap_is_shared_across_parallel_steps(self):
        message = SimpleNamespace(id=1, add_reaction=AsyncMock())
        context = SimpleNamespace(source_message=message, state=AgentTurnState())
        with patch(
            "elbow_helper.features.agent.discord_actions.reactions.require_evidence_access",
            AsyncMock(),
        ):
            results = await asyncio.gather(*(
                react_to_request(context, {"emoji": "👍"}) for _ in range(4)
            ))
        self.assertEqual(message.add_reaction.await_count, 3)
        self.assertEqual(sum("error" in result for result in results), 1)
        context.source_message = SimpleNamespace(id=0)
        self.assertEqual(
            await react_to_request(context, {"emoji": "👍"}),
            {"error": "There is no message to react to."},
        )
