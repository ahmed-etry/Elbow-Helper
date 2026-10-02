from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import discord
from elbow_helper.configuration.roles import LEAD
from elbow_helper.features.agent.capabilities.role_connections.commands import (
    prepare_connections, run_connections,
)
from elbow_helper.features.agent.models import AgentTurnState


class RoleConnectionCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_board_is_posted_only_after_its_preview(self):
        member = SimpleNamespace(id=4, roles=[SimpleNamespace(id=next(iter(LEAD)))])
        channel = SimpleNamespace(id=7, mention="#place")
        channel.permissions_for = lambda _: SimpleNamespace(
            view_channel=True, send_messages=True,
        )
        guild = SimpleNamespace(
            id=1, me=SimpleNamespace(id=5), get_member=lambda _: member,
            get_channel_or_thread=lambda _: channel,
        )
        channel.guild = guild
        message = SimpleNamespace(id=9)
        workflow = SimpleNamespace(
            connections_board_signature=lambda: "synthetic-state",
            build_connections_embed=lambda: discord.Embed(
                title="Role Connections", description="Synthetic rule",
            ),
            post_connections_message=AsyncMock(return_value=message),
        )
        context = SimpleNamespace(
            guild=guild, member=member, state=AgentTurnState(),
            source_message=SimpleNamespace(channel=channel),
            bot=SimpleNamespace(get_cog=lambda _: workflow),
        )
        with patch("elbow_helper.features.agent.capabilities.role_connections.commands.discord.TextChannel",
                   SimpleNamespace):
            preview = await prepare_connections(context, {})
            self.assertIn("Synthetic rule", preview.details)
            self.assertTrue(await preview.recheck())
            workflow.post_connections_message.assert_not_awaited()
            outcome = await run_connections(context, {})
        self.assertEqual(outcome.result["message_id"], 9)
        workflow.post_connections_message.assert_awaited_once_with(channel)
