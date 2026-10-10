"""Synthetic role settings preserve prior state and prohibit permission escalation."""

import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.features.agent.discord_actions.role_settings import (
    prepare_create, prepare_edit, undo_create, undo_edit,
)
from features.agent.discord_actions.management_helpers import fixture


class RoleSettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        access = patch("elbow_helper.features.agent.discord_actions.management."
                       "require_evidence_access", new_callable=AsyncMock)
        access.start()
        self.addCleanup(access.stop)

    def role(self):
        context, channel, role, member = fixture()
        role.name = "Synthetic role"
        role.colour = discord.Colour(0)
        role.hoist = False
        role.mentionable = False
        role.members = []
        role.delete = AsyncMock()

        async def edit(**values):
            for key, value in values.items():
                if key != "reason":
                    setattr(role, key, value)
            return role

        role.edit = AsyncMock(side_effect=edit)
        context.guild.create_role = AsyncMock(return_value=role)
        return context, channel, role, member

    async def test_role_edit_and_undo_restore_every_changed_field(self):
        context, _, role, _ = self.role()
        await prepare_edit(context, {"role_id": role.id, "name": "Synthetic renamed",
                                     "colour": 0x102030, "hoist": True, "position": 3,
                                     "permissions": {"send_messages": True}})
        role.edit.assert_not_called()
        action = context.state.proposed_changes[0]
        result = await action.run()
        self.assertEqual(role.colour.value, 0x102030)
        self.assertTrue(role.permissions.send_messages)
        undo = await undo_edit(context, {
            "targets": action.values, "before": action.preview.before, "after": result.after,
        })
        await undo.run()
        self.assertEqual(role.name, "Synthetic role")
        self.assertEqual(role.colour.value, 0)
        self.assertFalse(role.permissions.send_messages)

    async def test_creation_is_confirmed_and_undo_deletes_only_a_guarded_role(self):
        context, _, role, _ = self.role()
        await prepare_create(context, {"name": "Synthetic created", "position": 4})
        context.guild.create_role.assert_not_called()
        action = context.state.proposed_changes[0]
        result = await action.run()
        undo = await undo_create(context, {"targets": action.values, "after": result.after})
        await undo.run()
        role.delete.assert_awaited_once()

    async def test_administrator_unheld_permissions_and_positions_are_refused(self):
        for changes in ({"permissions": {"administrator": True}}, {"position": 50},
                        {"permissions": {"manage_messages": True}}):
            context, _, role, _ = self.role()
            context.member.guild_permissions = discord.Permissions(manage_roles=True)
            with self.assertRaises(ValueError):
                await prepare_edit(context, {"role_id": role.id, **changes})
            self.assertFalse(context.state.proposed_changes)
            role.edit.assert_not_called()

    async def test_everyone_and_permission_loss_after_preview_are_refused(self):
        context, _, role, _ = self.role()
        await prepare_edit(context, {"role_id": role.id, "hoist": True})
        context.guild.me.guild_permissions = discord.Permissions.none()
        with self.assertRaisesRegex(ValueError, "The bot needs Manage Roles"):
            await context.state.proposed_changes[0].run()
        role.edit.assert_not_called()
        context.guild.me.guild_permissions = discord.Permissions.all()
        role.id = context.guild.id
        with self.assertRaisesRegex(ValueError, "everyone role"):
            await prepare_edit(context, {"role_id": role.id, "name": "Synthetic"})
