"""Event panel changes run only after the agent's confirmation."""

from types import SimpleNamespace
from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.features.agent.access import ACCESS_LEAD
from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.capabilities.events.management import event_management_tools


class EventManagementActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_form_uses_feature_validation_and_waits_for_confirm(self):
        values = {"name": "Event", "start": "start", "end": "end", "timezone": "UTC"}
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 1, 2, tzinfo=timezone.utc)
        prepared = {"name": "Event", "start": start, "end": end,
                    "timezone": "UTC", "grace_hours": 24}
        workflow = SimpleNamespace(
            prepare_one_time_event_values=MagicMock(return_value=(prepared, None)),
            create_one_time_event=MagicMock(return_value="event-key"),
            force_refresh=AsyncMock(),
            build_event_detail_embed=MagicMock(return_value=SimpleNamespace(
                title="Event", description="Created", fields=[],
                footer=SimpleNamespace(text=None),
            )),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda name: workflow),
            guild=SimpleNamespace(), state=AgentTurnState(),
        )
        tool = next(tool for tool in event_management_tools()
                    if tool.definition.name == "create_event_tracker")
        with patch("elbow_helper.features.agent.capabilities.events.management.require_evidence_access",
                   new_callable=AsyncMock):
            result = await tool.handler(context, values)
        self.assertEqual(result["status"], "confirmation_required")
        action = context.state.proposed_changes[0]
        self.assertIs(action.action_class, ActionClass.CHANGE)
        self.assertIn(f"Start: Not set to <t:{int(start.timestamp())}:f>", action.preview.details)
        self.assertIn(f"End: Not set to <t:{int(end.timestamp())}:f>", action.preview.details)
        self.assertIn("Event Name: Not set to Event", action.preview.details)
        self.assertIn("Grace Period (Hours): Not set to 24", action.preview.details)
        self.assertTrue(any("voice channel" in line for line in action.preview.lines))
        self.assertFalse(any(str(int(start.timestamp())) in line for line in action.preview.lines))
        self.assertEqual(action.preview.detail_access, frozenset({ACCESS_LEAD}))
        self.assertEqual(tool.contract.required_access, frozenset({ACCESS_LEAD}))
        self.assertNotIn(str(int(start.timestamp())), str(result))
        workflow.create_one_time_event.assert_not_called()
        await action.run()
        workflow.create_one_time_event.assert_called_once_with(**prepared)
        workflow.force_refresh.assert_awaited_once()

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
        with patch("elbow_helper.features.agent.capabilities.events.management.require_evidence_access",
                   new_callable=AsyncMock):
            result = await tool.handler(context, {"event": "Alpha", "edge": "top"})
        self.assertEqual(result["status"], "confirmation_required")
        action = context.state.proposed_changes[0]
        self.assertIs(action.action_class, ActionClass.CHANGE)
        self.assertIn("position 4 to 1", action.preview.details[0])
        self.assertFalse(any("position 4 to 1" in line for line in action.preview.lines))
        self.assertIn("Alpha", action.preview.lines[0])
        workflow.move_event_to_position.assert_not_called()
        self.assertTrue(await action.preview.recheck())
        await action.run()
        workflow.move_event_to_position.assert_called_once_with("alpha", 0)
        workflow.force_refresh.assert_awaited_once()
