"""Synthetic channel lifecycle checks include empty-only creation undo and readable slowmode."""

import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.features.agent.discord_actions.channels import (
    duration, prepare_create, prepare_delete, prepare_edit, undo_create, undo_edit,
)
from features.agent.discord_actions.management_helpers import Entity, fixture


async def history(messages):
    for message in messages:
        yield message


class ChannelManagementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        access = patch("elbow_helper.features.agent.discord_actions.management."
                       "require_evidence_access", new_callable=AsyncMock)
        access.start()
        self.addCleanup(access.stop)

    def channel(self):
        context, channel, role, member = fixture()
        channel.type = discord.ChannelType.text
        channel.category_id = None
        channel.topic = "Synthetic topic"
        channel.slowmode_delay = 0
        channel.history = lambda **kwargs: history([])
        channel.delete = AsyncMock()

        async def edit(**values):
            for key in ("name", "topic", "slowmode_delay"):
                if key in values:
                    setattr(channel, key, values[key])

        channel.edit = AsyncMock(side_effect=edit)
        return context, channel, role, member

    async def test_edits_preview_readable_slowmode_and_restore_previous_settings(self):
        context, channel, _, _ = self.channel()
        await prepare_edit(context, {"channel_id": 200, "name": "renamed", "slowmode_seconds": 300})
        channel.edit.assert_not_called()
        action = context.state.proposed_changes[0]
        self.assertIn("Slowmode: 0 seconds to 5 minutes", action.preview.lines)
        result = await action.run()
        undo = await undo_edit(context, {
            "targets": action.values, "before": action.preview.before, "after": result.after,
        })
        await undo.run()
        self.assertEqual(channel.name, "synthetic")
        self.assertEqual(channel.slowmode_delay, 0)
        self.assertFalse(channel.edit.await_args.kwargs["sync_permissions"])

    async def test_creation_checks_initial_everyone_overwrites_and_undo_member_posts(self):
        context, channel, role, member = self.channel()
        role.id = context.guild.id
        role.position = 0
        context.guild.create_text_channel = AsyncMock(return_value=channel)
        await prepare_create(context, {"kind": "text", "name": "Synthetic private", "overwrites": [{
            "target_id": role.id, "target_type": "role", "operation": "set",
            "permissions": {"view_channel": False},
        }]})
        context.guild.create_text_channel.assert_not_called()
        action = context.state.proposed_changes[0]
        result = await action.run()
        overwrite = context.guild.create_text_channel.await_args.kwargs["overwrites"][role]
        self.assertFalse(overwrite.view_channel)
        undo = await undo_create(context, {"targets": action.values, "after": result.after})
        member.bot = False
        channel.history = lambda **kwargs: history([Entity(author=member)])
        self.assertFalse(await undo.preview.recheck())
        with self.assertRaisesRegex(ValueError, "member has posted"):
            await undo.run()
        channel.delete.assert_not_called()

    async def test_delete_is_irreversible_and_names_category_and_message_estimate(self):
        context, channel, _, _ = self.channel()
        channel.history = lambda **kwargs: history([Entity(), Entity()])
        await prepare_delete(context, {"channel_id": 200})
        action = context.state.proposed_changes[0]
        self.assertEqual(action.action_class.value, "irreversible")
        self.assertIn("Category: No category", action.preview.lines)
        self.assertIn("Messages: about 2", action.preview.lines)
        channel.delete.assert_not_called()
        await action.run()
        channel.delete.assert_awaited_once()

    def test_duration_units_are_readable(self):
        self.assertEqual([duration(value) for value in (1, 30, 300, 21600)],
                         ["1 second", "30 seconds", "5 minutes", "6 hours"])
