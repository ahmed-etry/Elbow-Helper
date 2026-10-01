"""Event panel changes run only after the agent's confirmation."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.tools.event_management import event_management_tools


class EventManagementActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_move_to_top_is_one_confirmed_feature_operation(self):
        state = {"event": {"key": "alpha", "name": "Alpha", "enabled": True,
                           "source": "preset", "category_id": None},
                 "position": 3, "count": 5}
        workflow = SimpleNamespace(
            event_management_state=lambda value: state,
            move_event_to_position=MagicMock(return_value=True),
            force_refresh=AsyncMock(),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda name: workflow),
            guild=SimpleNamespace(), state=AgentTurnState(),
        )
        tool = next(tool for tool in event_management_tools()
                    if tool.definition.name == "move_event")
        with patch("elbow_helper.features.agent.tools.event_management.require_evidence_access",
                   new_callable=AsyncMock):
            result = await tool.handler(context, {"event": "Alpha", "edge": "top"})
        self.assertEqual(result["status"], "confirmation_required")
        action = context.state.command_proposals[0]
        self.assertIs(action.action_class, ActionClass.CHANGE)
        self.assertIn("position 4 to 1", action.preview.lines[0])
        workflow.move_event_to_position.assert_not_called()
        self.assertTrue(await action.preview.recheck())
        await action.run()
        workflow.move_event_to_position.assert_called_once_with("alpha", 0)
        workflow.force_refresh.assert_awaited_once()
