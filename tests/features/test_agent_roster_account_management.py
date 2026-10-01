"""Roster account picker changes share the feature's selection operation."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.tools.roster_account_management import (
    prepare_roster_signup, prepare_roster_row_removal,
)
from elbow_helper.features.rosters.models import LinkedAccount, RosterMember


class RosterAccountManagementTests(unittest.IsolatedAsyncioTestCase):
    async def test_removal_accepts_a_signup_without_a_current_account_link(self):
        roster = SimpleNamespace(id=17, guild_id=1, role_id=None, name="War")
        signup = RosterMember("#P2", 4, "Player", "BEH", 16, 1)
        state = {"roster": roster, "members": (signup,), "posts": ((9, 91),)}
        workflow = SimpleNamespace(
            roster_signed_rows=AsyncMock(return_value=state),
            remove_roster_signup_rows=AsyncMock(return_value=SimpleNamespace(
                changed=True, message="Removed 1 account.")),
        )
        bot_member = SimpleNamespace(id=999, top_role=SimpleNamespace(position=10))
        owner = SimpleNamespace(id=4, top_role=SimpleNamespace(position=1))
        guild = SimpleNamespace(id=1, me=bot_member, get_role=lambda role_id: None,
                                get_member=lambda member_id: owner)
        channel = SimpleNamespace(guild=guild, permissions_for=lambda actor: SimpleNamespace(
            view_channel=True, send_messages=True))
        guild.get_channel_or_thread = lambda channel_id: channel
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda name: workflow), guild=guild,
            member=owner, state=AgentTurnState(),
        )
        with patch("elbow_helper.features.agent.tools.roster_account_management.require_evidence_access",
                   new_callable=AsyncMock):
            result = await prepare_roster_row_removal(
                context, {"roster_id": 17, "accounts": ["Player"]})
        self.assertEqual(result["status"], "confirmation_required")
        action = context.state.command_proposals[0]
        self.assertTrue(any("#P2" in line for line in action.preview.lines))
        self.assertTrue(await action.preview.recheck())
        await action.run()
        workflow.remove_roster_signup_rows.assert_awaited_once_with(17, ["#P2"])

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
        member = SimpleNamespace(id=4, mention="<@4>",
                                 top_role=SimpleNamespace(position=1))
        bot_member = SimpleNamespace(id=999, top_role=SimpleNamespace(position=10))
        guild = SimpleNamespace(id=1, me=bot_member, get_role=lambda role_id: None)
        channel = SimpleNamespace(guild=guild, permissions_for=lambda actor: SimpleNamespace(
            view_channel=True, send_messages=True))
        guild.get_channel_or_thread = lambda channel_id: channel
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda name: workflow),
            guild=guild,
            member=member, state=AgentTurnState(),
        )
        with (patch("elbow_helper.features.agent.tools.roster_account_management.require_evidence_access",
                    new_callable=AsyncMock),
              patch("elbow_helper.features.agent.tools.roster_account_management.resolve_member",
                    new_callable=AsyncMock, return_value=member)):
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
