"""Synthetic guild event permissions, entity types, confirmation and undo."""

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.agent.discord_actions.scheduled_events import (
    prepare_cancel, prepare_create, prepare_edit, undo_edit,
)
from agent.discord_actions.management_helpers import Entity, fixture


class ScheduledEventTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        access = patch("elbow_helper.agent.discord_actions.management."
                       "require_evidence_access", new_callable=AsyncMock)
        access.start()
        self.addCleanup(access.stop)

    def context(self):
        context, channel, _, _ = fixture()
        start = datetime.now(timezone.utc) + timedelta(days=7)
        event = Entity(id=800, name="Synthetic event", description="Synthetic description",
                       entity_type=discord.EntityType.external, channel_id=None,
                       location="Synthetic location", start_time=start,
                       end_time=start + timedelta(hours=1), status=discord.EventStatus.scheduled,
                       creator_id=777)

        async def edit(**values):
            for key, value in values.items():
                if key != "reason":
                    setattr(event, key, value)
            return event

        event.edit = AsyncMock(side_effect=edit)
        context.guild.fetch_scheduled_event = AsyncMock(return_value=event)
        context.guild.create_scheduled_event = AsyncMock(return_value=event)
        return context, channel, event

    async def test_external_creation_omits_channel_and_waits_for_confirmation(self):
        context, _, event = self.context()
        await prepare_create(context, {"name": event.name, "kind": "external",
                                       "location": event.location,
                                       "start_time": event.start_time.isoformat(),
                                       "end_time": event.end_time.isoformat()})
        context.guild.create_scheduled_event.assert_not_called()
        await context.state.proposed_changes[0].run()
        self.assertNotIn("channel", context.guild.create_scheduled_event.await_args.kwargs)

    async def test_edits_restore_previous_state_and_cancel_is_irreversible(self):
        context, _, event = self.context()
        await prepare_edit(context, {"event_id": event.id, "name": "Synthetic renamed"})
        action = context.state.proposed_changes[0]
        result = await action.run()
        self.assertNotIn("start_time", event.edit.await_args.kwargs)
        undo = await undo_edit(context, {
            "targets": action.values, "before": action.preview.before, "after": result.after,
        })
        await undo.run()
        self.assertEqual(event.name, "Synthetic event")
        await prepare_cancel(context, {"event_id": event.id})
        cancel = context.state.proposed_changes[-1]
        self.assertEqual(cancel.action_class.value, "irreversible")
        await cancel.run()
        self.assertEqual(event.status, discord.EventStatus.canceled)

    async def test_create_events_is_enough_for_own_event_but_not_another_members(self):
        context, _, event = self.context()
        context.member.guild_permissions = discord.Permissions(create_events=True)
        with self.assertRaisesRegex(ValueError, "You need Manage Events"):
            await prepare_edit(context, {"event_id": event.id, "name": "Synthetic"})
        event.creator_id = context.member.id
        await prepare_edit(context, {"event_id": event.id, "name": "Synthetic"})
        self.assertEqual(len(context.state.proposed_changes), 1)

    async def test_stage_requires_its_channel_permissions_before_preview(self):
        context, channel, event = self.context()
        channel.type = discord.ChannelType.stage_voice
        context.guild.me.guild_permissions = discord.Permissions(
            create_events=True, view_channel=True,
        )
        with self.assertRaisesRegex(ValueError, "Manage Channels, Mute Members, Move Members"):
            await prepare_create(context, {"name": "Synthetic stage", "kind": "stage",
                                           "channel_id": channel.id,
                                           "start_time": event.start_time.isoformat()})
        context.guild.create_scheduled_event.assert_not_called()
