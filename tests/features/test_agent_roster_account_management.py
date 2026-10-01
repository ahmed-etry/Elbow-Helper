"""Roster account picker changes share the feature's selection operation."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.tools.roster_account_management import prepare_roster_signup
from elbow_helper.features.rosters.models import LinkedAccount


class RosterAccountManagementTests(unittest.IsolatedAsyncioTestCase):
    async def test_signup_previews_selected_account_before_feature_changes_it(self):
        account = LinkedAccount("#P2", "Player", "BEH", 16)
        roster = SimpleNamespace(id=17, guild_id=1, role_id=None, name="War")
        picker = SimpleNamespace(accounts=(account,), message="")
        workflow = SimpleNamespace(
            prepare_roster_account_selection=AsyncMock(return_value=(roster, picker)),
            resolve_roster_account_choices=lambda accounts, names: (["#P2"], None),
            roster_edit_state=AsyncMock(return_value={"posts": ((9, 91),)}),
            change_roster_accounts=AsyncMock(return_value=SimpleNamespace(
                changed=True, message="Signed up Player.")),
        )
        member = SimpleNamespace(id=4, mention="<@4>")
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda name: workflow),
            guild=SimpleNamespace(id=1, me=object(), get_role=lambda role_id: None),
            member=member, state=AgentTurnState(),
        )
        with (patch("elbow_helper.features.agent.tools.roster_account_management.require_evidence_access",
                    new_callable=AsyncMock),
              patch("elbow_helper.features.agent.tools.roster_account_management.resolve_member",
                    new_callable=AsyncMock, return_value=member),
              patch("elbow_helper.features.agent.tools.roster_account_management.check_member")):
            result = await prepare_roster_signup(
                context, {"roster_id": 17, "accounts": ["Player"]})
        self.assertEqual(result["status"], "confirmation_required")
        action = context.state.command_proposals[0]
        self.assertTrue(any("#P2" in line for line in action.preview.lines))
        self.assertTrue(any("<#9>" in line for line in action.preview.lines))
        workflow.change_roster_accounts.assert_not_awaited()
        self.assertTrue(await action.preview.recheck())
        outcome = await action.run()
        self.assertEqual(outcome.text, "Signed up Player.")
        workflow.change_roster_accounts.assert_awaited_once()
