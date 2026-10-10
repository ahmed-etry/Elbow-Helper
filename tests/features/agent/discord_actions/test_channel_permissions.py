"""Synthetic overwrite actions require confirmation, guard real targets and undo exactly."""

import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.features.agent.discord_actions.channel_permissions import (
    channel_permission_tools, prepare_permissions, undo_permissions,
)
from elbow_helper.features.agent.plan.checker import valid_arguments
from features.agent.discord_actions.management_helpers import fixture


class ChannelPermissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.access = patch("elbow_helper.features.agent.discord_actions.management."
                            "require_evidence_access", new_callable=AsyncMock)
        self.access.start()
        self.addCleanup(self.access.stop)

    def arguments(self, target_id=30):
        return {"channel_id": 200, "targets": [{
            "target_type": "role", "target_id": target_id, "operation": "change",
            "permissions": {"send_messages": False},
        }]}

    async def test_preview_precedes_writes_and_undo_restores_overwrite(self):
        context, channel, role, _ = fixture()
        original = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        channel.overwrites[role] = original
        await prepare_permissions(context, self.arguments())
        channel.set_permissions.assert_not_called()
        action = context.state.proposed_changes[0]
        self.assertIn("Send Messages: Allow to Deny", action.preview.lines)
        result = await action.run()
        self.assertFalse(channel.overwrites[role].send_messages)
        undo = await undo_permissions(context, {
            "targets": action.values, "before": action.preview.before, "after": result.after,
        })
        await undo.run()
        self.assertEqual(channel.overwrites[role], original)

    async def test_missing_bot_permission_and_requester_hierarchy_are_refused(self):
        for bot_permission, position in ((False, 2), (True, 50), (True, 100)):
            context, channel, role, _ = fixture()
            role.position = position
            if not bot_permission:
                context.guild.me.guild_permissions = discord.Permissions(view_channel=True)
            with self.assertRaises(ValueError) as error:
                await prepare_permissions(context, self.arguments())
            if not bot_permission:
                self.assertIn("The bot needs Manage Permissions", str(error.exception))
            self.assertFalse(context.state.proposed_changes)
            channel.set_permissions.assert_not_called()

    async def test_everyone_can_be_denied_view_channel(self):
        context, channel, role, _ = fixture()
        role.id = context.guild.id
        role.position = 0
        context.member.top_role.position = 0
        role.is_default = lambda: True
        arguments = self.arguments(role.id)
        arguments["targets"][0]["permissions"] = {"view_channel": False}
        await prepare_permissions(context, arguments)
        await context.state.proposed_changes[0].run()
        self.assertFalse(channel.overwrites[role].view_channel)

    async def test_grants_and_changed_permissions_are_checked_again_after_preview(self):
        context, channel, _, _ = fixture()
        await prepare_permissions(context, self.arguments())
        action = context.state.proposed_changes[0]
        context.member.guild_permissions = discord.Permissions(view_channel=True)
        self.assertFalse(await action.preview.recheck())
        with self.assertRaises(ValueError):
            await action.run()
        channel.set_permissions.assert_not_called()
        context.member.guild_permissions = discord.Permissions(manage_roles=True, view_channel=True)
        arguments = self.arguments()
        arguments["targets"][0]["permissions"] = {"send_messages": True}
        with self.assertRaisesRegex(ValueError, "cannot grant"):
            await prepare_permissions(context, arguments)

    def test_schema_checks_permission_values_before_execution(self):
        schema = channel_permission_tools()[0].definition.parameters
        self.assertTrue(valid_arguments(self.arguments(), schema))
        arguments = self.arguments()
        arguments["targets"][0]["permissions"] = {"send_messages": "yes"}
        self.assertFalse(valid_arguments(arguments, schema))

    async def test_unknown_inherited_permission_is_refused_before_preview(self):
        context, channel, _, _ = fixture()
        arguments = self.arguments()
        arguments["targets"][0]["permissions"] = {"synthetic_permission": None}
        with self.assertRaisesRegex(ValueError, "These permissions are invalid"):
            await prepare_permissions(context, arguments)
        self.assertFalse(context.state.proposed_changes)
        channel.set_permissions.assert_not_called()
