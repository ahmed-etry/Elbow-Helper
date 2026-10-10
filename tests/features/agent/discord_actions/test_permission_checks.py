"""Synthetic permission facts distinguish targets, actors and hierarchy boundaries."""

from types import SimpleNamespace
import unittest

import discord

from elbow_helper.features.agent.discord_actions.permission_checks import (
    below_both_roles, missing_permissions, unheld_permissions,
)


def actor(position, **permissions):
    return SimpleNamespace(
        top_role=SimpleNamespace(position=position),
        guild_permissions=discord.Permissions(**permissions),
    )


class PermissionCheckTests(unittest.TestCase):
    def test_channel_permissions_override_server_permissions(self):
        requester = actor(8, manage_roles=True, manage_channels=True)
        channel = SimpleNamespace(permissions_for=lambda _: discord.Permissions(manage_roles=True))
        self.assertEqual(missing_permissions(
            requester, ("manage_roles", "manage_channels"), channel=channel,
        ), ("manage_channels",))
        self.assertEqual(missing_permissions(requester, ("manage_channels",)), ())

    def test_every_missing_permission_is_reported(self):
        requester = actor(8, view_channel=True)
        names = ("view_channel", "manage_messages", "read_message_history")
        self.assertEqual(missing_permissions(requester, names), names[1:])
        self.assertEqual(missing_permissions(None, names), names)

    def test_hierarchy_checks_both_actors_and_refuses_equal_positions(self):
        for position, asker_position, bot_position, allowed in (
            (3, 4, 5, True), (4, 4, 5, False), (5, 6, 5, False),
            (7, 6, 8, False), (7, 8, 6, False),
        ):
            with self.subTest(position=position, asker=asker_position, bot=bot_position):
                self.assertEqual(below_both_roles(
                    SimpleNamespace(position=position), actor(asker_position), actor(bot_position),
                ), allowed)
        self.assertFalse(below_both_roles(None, actor(8), actor(10)))

    def test_permission_grants_use_the_target_channel(self):
        requester = actor(8, send_messages=True, manage_roles=True)
        channel = SimpleNamespace(permissions_for=lambda _: discord.Permissions(manage_roles=True))
        granted = discord.Permissions(send_messages=True, manage_roles=True)
        self.assertEqual(unheld_permissions(granted, requester), ())
        self.assertEqual(
            unheld_permissions(granted, requester, channel=channel), ("send_messages",),
        )

    def test_administrator_expands_permissions_for_existing_actors(self):
        requester = actor(8, administrator=True)
        channel = SimpleNamespace(permissions_for=lambda _: discord.Permissions.all())
        self.assertEqual(
            unheld_permissions(discord.Permissions.all(), requester, channel=channel), (),
        )
