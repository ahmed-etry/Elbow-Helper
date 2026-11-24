"""Event panel changes run only after the agent's confirmation."""

from types import SimpleNamespace
from datetime import datetime, timezone
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from elbow_helper.agent.safety.access import ACCESS_LEAD
from elbow_helper.agent.actions.contracts import ActionClass
from elbow_helper.agent.engine.models import AgentTurnState
from elbow_helper.agent.features.event_stats.management import event_management_tools


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
            event_management_state=MagicMock(return_value={"event": {"name": "Event"}}),
        )
        context = SimpleNamespace(private_pass=True,
            bot=SimpleNamespace(get_cog=lambda name: workflow),
            guild=SimpleNamespace(), state=AgentTurnState(),
        )
        tool = next(tool for tool in event_management_tools()
                    if tool.definition.name == "create_event_tracker")
        with patch('elbow_helper.agent.features.event_stats.management.require_evidence_access',
                   new_callable=AsyncMock):
            result = await tool.handler(context, values)
        self.assertEqual(result["status"], "confirmation_required")
        action = context.state.proposed_changes[0]
        self.assertIs(action.action_class, ActionClass.CHANGE)
        self.assertIn(f"Start goes from Not set to <t:{int(start.timestamp())}:f>.", action.preview.details)
        self.assertIn(f"End goes from Not set to <t:{int(end.timestamp())}:f>.", action.preview.details)
        self.assertIn("Event Name goes from Not set to Event.", action.preview.details)
        self.assertIn("Grace Period (Hours) goes from Not set to 24.", action.preview.details)
        self.assertTrue(any("voice channel" in line for line in action.preview.lines))
        self.assertFalse(any(str(int(start.timestamp())) in line for line in action.preview.lines))
        self.assertEqual(action.preview.detail_access, frozenset({ACCESS_LEAD}))
        self.assertEqual(tool.contract.required_access, frozenset({ACCESS_LEAD}))
        self.assertNotIn(str(int(start.timestamp())), str(result))
        workflow.create_one_time_event.assert_not_called()
        outcome = await action.run()
        self.assertEqual(outcome.text, "Created event tracker **Event**.")
        workflow.create_one_time_event.assert_called_once_with(**prepared)
        workflow.event_management_state.assert_called_once_with("event-key")
        workflow.force_refresh.assert_awaited_once()

    async def test_confirmed_changes_save_off_the_event_loop(self):
        saved_on = []

        def save(result):
            def mutate(*args, **kwargs):
                saved_on.append(threading.get_ident())
                return result
            return mutate

        event = {"key": "alpha", "name": "Alpha", "enabled": True, "source": "custom",
                 "category_id": None, "grace_period_hours": 24}
        preset = {**event, "source": "preset"}
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        prepared = {"name": "Event", "start": start, "end": start, "timezone": "UTC", "grace_hours": 24}
        embed = SimpleNamespace(title="Event", description="Done", fields=[],
                                footer=SimpleNamespace(text=None))
        workflow = SimpleNamespace(
            event_management_state=lambda value: {"event": preset if value == "preset" else event,
                                                  "position": 0, "count": 2},
            prepare_one_time_event_values=MagicMock(return_value=(prepared, None)),
            create_one_time_event=save("key"), update_one_time_event=save(True),
            update_preset_event=save(True), toggle_event=save(True),
            set_event_category=save(True), move_event_to_position=save(True),
            reset_preset_event=save(True),
            force_refresh=AsyncMock(), build_event_detail_embed=MagicMock(return_value=embed),
        )
        tools = {tool.definition.name: tool for tool in event_management_tools()}
        category = MagicMock(spec=discord.CategoryChannel)
        category.name = "Events"
        calls = (
            ("create_event_tracker", {"name": "Event", "start": "s", "end": "e", "timezone": "UTC"}),
            ("edit_event_tracker", {"event": "Alpha", "name": "Event", "start": "s", "end": "e",
                                    "timezone": "UTC"}),
            ("edit_preset_event", {"event": "preset", "name": "Renamed"}),
            ("set_event_enabled", {"event": "Alpha", "enabled": False}),
            ("set_event_category", {"event": "Alpha", "category_id": 5}),
            ("move_event", {"event": "Alpha", "position": 2}),
            ("restore_event_defaults", {"event": "preset"}),
        )
        for name, values in calls:
            context = SimpleNamespace(
                private_pass=True, bot=SimpleNamespace(get_cog=lambda cog: workflow),
                guild=SimpleNamespace(get_channel=lambda channel_id: category), state=AgentTurnState())
            with patch("elbow_helper.agent.features.event_stats.management.require_evidence_access",
                       new_callable=AsyncMock):
                result = await tools[name].handler(context, values)
            self.assertEqual(result["status"], "confirmation_required", name)
            await context.state.proposed_changes[0].run()
        self.assertEqual(len(saved_on), 7)
        self.assertNotIn(threading.get_ident(), saved_on)

    async def test_preset_edit_refuses_values_the_save_would_cut(self):
        # Asked for 500 grace hours, the preview promised 500 and the save kept 168.
        from elbow_helper.agent.actions.contracts import ActionRefused
        event = {"key": "preset", "name": "Preset", "enabled": True, "source": "preset",
                 "category_id": None, "grace_period_hours": 24}
        workflow = SimpleNamespace(event_management_state=lambda value: {"event": event, "position": 0,
                                                                         "count": 1},
                                   update_preset_event=MagicMock(return_value=True))
        tool = next(tool for tool in event_management_tools() if tool.definition.name == "edit_preset_event")
        for values, refusal in (({"name": "N" * 150}, "Enter an event name."),
                                ({"name": "Preset", "grace_hours": 500},
                                 "Grace hours must be a whole number between 0 and 168.")):
            with self.subTest(values=list(values)):
                context = SimpleNamespace(private_pass=True, bot=SimpleNamespace(get_cog=lambda name: workflow),
                                          guild=SimpleNamespace(), state=AgentTurnState())
                with patch('elbow_helper.agent.features.event_stats.management.require_evidence_access',
                           new_callable=AsyncMock), self.assertRaises(ActionRefused) as raised:
                    await tool.handler(context, {"event": "preset", **values})
                self.assertEqual(str(raised.exception), refusal)
                self.assertEqual(context.state.proposed_changes, [])

    async def test_move_to_top_is_one_confirmed_feature_operation(self):
        state = {"event": {"key": "alpha", "name": "Alpha", "enabled": True,
                           "source": "preset", "category_id": None},
                 "position": 3, "count": 5}
        workflow = SimpleNamespace(
            event_management_state=lambda value: state,
            move_event_to_position=MagicMock(return_value=True),
            force_refresh=AsyncMock(),
        )
        context = SimpleNamespace(private_pass=True,
            bot=SimpleNamespace(get_cog=lambda name: workflow),
            guild=SimpleNamespace(), state=AgentTurnState(),
        )
        tool = next(tool for tool in event_management_tools()
                    if tool.definition.name == "move_event")
        with patch('elbow_helper.agent.features.event_stats.management.require_evidence_access',
                   new_callable=AsyncMock):
            result = await tool.handler(context, {"event": "Alpha", "edge": "top"})
        self.assertEqual(result["status"], "confirmation_required")
        action = context.state.proposed_changes[0]
        self.assertIs(action.action_class, ActionClass.CHANGE)
        self.assertIn("position 4 to 1", action.preview.details[0])
        self.assertFalse(any("position 4 to 1" in line for line in action.preview.lines))
        self.assertEqual(action.preview.details[0], "⚙️ Move **Alpha** from position 4 to 1")
        workflow.move_event_to_position.assert_not_called()
        self.assertTrue(await action.preview.recheck())
        await action.run()
        workflow.move_event_to_position.assert_called_once_with("alpha", 0)
        workflow.force_refresh.assert_awaited_once()
