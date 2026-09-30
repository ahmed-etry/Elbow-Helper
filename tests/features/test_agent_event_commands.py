"""The agent opens the event management screen through its feature."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.roles import LEAD
from elbow_helper.features.agent.commands.adapters.event_stats import run_event_panel
from elbow_helper.features.agent.models import AgentTurnState


class EventCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_panel_uses_feature_and_rechecks_current_access(self):
        member = SimpleNamespace(id=4, roles=[SimpleNamespace(id=next(iter(LEAD)))])
        guild = SimpleNamespace(get_member=lambda _: member)
        workflow = object()
        context = SimpleNamespace(
            guild=guild, member=member, state=AgentTurnState(),
            bot=SimpleNamespace(get_cog=lambda _: workflow),
        )
        outcome = await run_event_panel(context, {})
        self.assertEqual(outcome.visibility, "private")
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        with patch("elbow_helper.features.agent.commands.adapters.event_stats.open_event_panel",
                   new_callable=AsyncMock) as open_panel:
            await outcome.private_panel(interaction)
            open_panel.assert_awaited_once_with(interaction, workflow, guild)
            member.roles = []
            await outcome.private_panel(interaction)
            open_panel.assert_awaited_once()
        interaction.response.send_message.assert_awaited_once()
