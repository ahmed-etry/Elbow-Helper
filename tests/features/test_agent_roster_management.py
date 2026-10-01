"""Panel roster changes use feature operations after a complete preview."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.tools.roster_management import roster_management_tools


class RosterManagementActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_clear_previews_members_and_posts_before_calling_feature(self):
        roster = SimpleNamespace(
            id=17, guild_id=1, name="War", status="open", buttons_hidden=False,
            active_cycle_id=3, role_id=8,
        )
        snapshot = {"roster": roster, "account_count": 3,
                    "member_ids": (41, 42), "posts": ((9, 91),)}
        workflow = SimpleNamespace(
            roster_management_state=AsyncMock(return_value=snapshot),
            clear_roster_signups=AsyncMock(return_value=SimpleNamespace(message="Cleared signups.")),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda name: workflow),
            guild=SimpleNamespace(id=1, get_role=lambda role_id: SimpleNamespace(mention="<@&8>")),
            state=AgentTurnState(),
        )
        tool = next(tool for tool in roster_management_tools()
                    if tool.definition.name == "clear_roster_signups")
        with patch("elbow_helper.features.agent.tools.roster_management.require_evidence_access",
                   new_callable=AsyncMock):
            result = await tool.handler(context, {"roster_id": 17})
        self.assertEqual(result["status"], "confirmation_required")
        action = context.state.command_proposals[0]
        self.assertIs(action.action_class, ActionClass.IRREVERSIBLE)
        self.assertTrue(any("<@41>" in line for line in action.preview.lines))
        self.assertTrue(any("<@42>" in line for line in action.preview.lines))
        self.assertTrue(any("<@&8>" in line for line in action.preview.lines))
        self.assertTrue(any("<@9>" not in line and "<#9>" in line
                            for line in action.preview.lines))
        workflow.clear_roster_signups.assert_not_awaited()
        self.assertTrue(await action.preview.recheck())
        outcome = await action.run()
        self.assertEqual(outcome.text, "Cleared signups.")
        workflow.clear_roster_signups.assert_awaited_once_with(17)
