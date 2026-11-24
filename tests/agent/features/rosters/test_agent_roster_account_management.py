"""Roster account picker changes share the feature's selection operation."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.actions.details import prepare_preview
from elbow_helper.agent.actions.preview import ConfirmationView
from elbow_helper.agent.actions.contracts import ActionRefused
from elbow_helper.agent.engine.capability_contract import compile_capability_call
from elbow_helper.agent.engine.models import AgentTurnState
from elbow_helper.agent.engine.tool_call import execute_tool
from elbow_helper.agent.features.rosters.signups import (
    prepare_roster_signup, prepare_roster_row_removal, roster_account_management_tools,
)
from elbow_helper.configuration.roles import CO_LEADER_ROLE_ID
from elbow_helper.features.rosters.models import LinkedAccount, RosterMember
from elbow_helper.features.rosters.services.membership import MembershipResult
from tests.agent.discord_actions.helpers import register_requester


class RosterAccountManagementTests(unittest.IsolatedAsyncioTestCase):
    async def test_lead_can_prepare_bulk_signup_in_a_leadership_room(self):
        roster = SimpleNamespace(id=1, guild_id=1, role_id=None, name="CWL Signup")
        workflow = SimpleNamespace(bulk_add_roster_preview=AsyncMock(return_value={
            "roster": roster, "accounts": (("#P2", "Player", 4, False),), "posts": (),
        }))
        member = SimpleNamespace(id=4, mention="<@4>")
        requester = SimpleNamespace(id=5, roles=[SimpleNamespace(id=CO_LEADER_ROLE_ID)])
        guild = SimpleNamespace(id=1, me=SimpleNamespace(id=999),
                                get_member={4: member, 5: requester}.get)
        scope = SimpleNamespace(levels=frozenset({"lead_plus"}))
        context = register_requester(SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda name: workflow), guild=guild,
            member=requester, state=AgentTurnState(scope=scope), source_message=None,
        ))
        tool = next(tool for tool in roster_account_management_tools()
                    if tool.definition.name == "bulk_add_roster_accounts")
        arguments = {"roster_id": 1, "player_tags": ["#P2"]}

        async def prepare():
            return json.loads(await execute_tool(
                name=tool.definition.name, handler=tool.handler, arguments=arguments,
                capability_scope=compile_capability_call(tool, arguments),
                context=context, action_class=tool.action_class,
            ))

        with (patch("elbow_helper.agent.engine.tool_call.require_evidence_access",
                    new_callable=AsyncMock),
              patch("elbow_helper.agent.features.rosters.signups.require_evidence_access",
                    new_callable=AsyncMock),
              patch("elbow_helper.agent.safety.disclosure.request_scope",
                    new=AsyncMock(return_value=scope))):
            self.assertEqual((await prepare())["status"], "confirmation_required")
            workflow.bulk_add_roster_preview.assert_awaited_once_with(1, "#P2")
            await prepare_preview(context)
            action, = context.state.proposed_changes
            self.assertFalse(action.details_hidden)
            self.assertTrue(await action.preview.recheck())
            requester.roles.clear()
            workflow.bulk_add_roster_preview.reset_mock()
            self.assertFalse(await action.preview.recheck())
            self.assertEqual((await prepare())["cause"], "access_denied")
            workflow.bulk_add_roster_preview.assert_not_awaited()

    async def test_removal_accepts_a_signup_without_a_current_account_link(self):
        roster = SimpleNamespace(id=17, guild_id=1, role_id=None, name="War")
        signup = RosterMember("#P2", 4, "Player", "BEH", 16, 1)
        state = {"roster": roster, "members": (signup,), "posts": ((9, 91),)}
        workflow = SimpleNamespace(
            roster_signed_rows=AsyncMock(return_value=state),
            remove_roster_signup_rows=AsyncMock(return_value=MembershipResult(
                "Removed 1 account.", changed=True, affected_tags=("#P2",))),
        )
        bot_member = SimpleNamespace(id=999, top_role=SimpleNamespace(position=10))
        owner = SimpleNamespace(id=4, top_role=SimpleNamespace(position=1))
        bot_permissions = SimpleNamespace(view_channel=True, send_messages=True)
        asker_permissions = SimpleNamespace(view_channel=True, send_messages=False)
        guild = SimpleNamespace(id=1, me=bot_member, get_role=lambda role_id: None,
                                get_member=lambda member_id: owner)
        channel = SimpleNamespace(guild=guild, permissions_for=lambda actor:
                                  bot_permissions if actor is bot_member else asker_permissions)
        guild.get_channel_or_thread = lambda channel_id: channel
        requester = SimpleNamespace(id=5, roles=[SimpleNamespace(id=CO_LEADER_ROLE_ID)])
        context = SimpleNamespace(private_pass=True,
            bot=SimpleNamespace(get_cog=lambda name: workflow), guild=guild,
            member=requester, state=AgentTurnState(),
        )
        context = register_requester(context)
        with patch('elbow_helper.agent.features.rosters.signups.require_evidence_access',
                   new_callable=AsyncMock):
            result = await prepare_roster_row_removal(
                context, {"roster_id": 17, "accounts": ["Player"]})
        self.assertEqual(result["status"], "confirmation_required")
        action = context.state.proposed_changes[0]
        self.assertTrue(any("#P2" in line for line in action.preview.lines))
        self.assertTrue(await action.preview.recheck())
        asker_permissions.view_channel = False
        self.assertTrue(await action.preview.recheck())
        bot_permissions.send_messages = False
        self.assertFalse(await action.preview.recheck())
        bot_permissions.send_messages = True
        await action.run()
        workflow.remove_roster_signup_rows.assert_awaited_once_with(17, ["#P2"])

    async def test_signup_previews_selected_account_before_feature_changes_it(self):
        account = LinkedAccount("#P2", "Player", "BEH", 16)
        role = SimpleNamespace(id=20, mention="<@&20>", is_default=lambda: False)
        roster = SimpleNamespace(id=17, guild_id=1, role_id=role.id, name="War")
        picker = SimpleNamespace(accounts=(account,), message="")
        workflow = SimpleNamespace(
            prepare_roster_account_selection=AsyncMock(return_value=(roster, picker)),
            resolve_roster_account_choices=lambda accounts, names: (["#P2"], None),
            roster_edit_state=AsyncMock(return_value={"posts": ((9, 91),)}),
            roster_signup_role_changes=AsyncMock(return_value=True),
            change_roster_accounts=AsyncMock(return_value=MembershipResult(
                "Signed up Player.", changed=True, affected_tags=("#P2",))),
        )
        member = SimpleNamespace(id=4, mention="<@4>",
                                 top_role=SimpleNamespace(position=1))
        bot_member = SimpleNamespace(id=999, top_role=SimpleNamespace(position=10))
        guild = SimpleNamespace(id=1, me=bot_member, get_role=lambda role_id: role)
        channel = SimpleNamespace(guild=guild, permissions_for=lambda actor: SimpleNamespace(
            view_channel=True, send_messages=True))
        guild.get_channel_or_thread = lambda channel_id: channel
        context = SimpleNamespace(private_pass=True,
            bot=SimpleNamespace(get_cog=lambda name: workflow),
            guild=guild,
            member=member, state=AgentTurnState(),
        )
        context = register_requester(context)
        with (patch('elbow_helper.agent.features.rosters.signups.require_evidence_access',
                    new_callable=AsyncMock),
              patch('elbow_helper.agent.features.rosters.signups.resolve_member',
                    new_callable=AsyncMock, return_value=member)):
            result = await prepare_roster_signup(
                context, {"roster_id": 17, "accounts": ["Player"]})
            workflow.roster_signup_role_changes.return_value = False
            await prepare_roster_signup(context, {"roster_id": 17, "accounts": ["Player"]})
        self.assertEqual(result["status"], "confirmation_required")
        action, with_role_already_held = context.state.proposed_changes
        expected = "📝 Sign up **Player** `#P2` for **War**"
        card = ConfirmationView(member.id, (action,), context)
        self.assertEqual(card.card.description, expected + "\nYou'll get <@&20>")
        card = ConfirmationView(member.id, (with_role_already_held,), context)
        self.assertEqual(card.card.description, expected)
        workflow.change_roster_accounts.assert_not_awaited()
        self.assertFalse(await action.preview.recheck())
        workflow.roster_signup_role_changes.return_value = True
        self.assertTrue(await action.preview.recheck())
        outcome = await action.run()
        self.assertEqual(outcome.text, "**Player** `#P2` is signed up for **War**.")
        workflow.change_roster_accounts.assert_awaited_once()

    async def test_signup_uses_panel_role_rules_for_own_and_other_accounts(self):
        account = LinkedAccount("#P2", "Player", "BEH", 16)
        roster = SimpleNamespace(id=17, guild_id=1, role_id=None, name="War")
        picker = SimpleNamespace(accounts=(account,), message="")
        workflow = SimpleNamespace(
            prepare_roster_account_selection=AsyncMock(return_value=(roster, picker)),
            resolve_roster_account_choices=lambda accounts, names: (["#P2"], None),
            roster_edit_state=AsyncMock(return_value={"posts": ((9, 91),)}),
        )
        member = SimpleNamespace(id=4, mention="<@4>", roles=[], top_role=SimpleNamespace(position=1))
        other = SimpleNamespace(id=5, mention="<@5>", top_role=SimpleNamespace(position=1))
        bot_member = SimpleNamespace(id=999, top_role=SimpleNamespace(position=10))
        guild = SimpleNamespace(id=1, me=bot_member, get_role=lambda role_id: None,
                                get_member={other.id: other}.get)
        channel = SimpleNamespace(guild=guild, permissions_for=lambda actor: SimpleNamespace(
            view_channel=actor is bot_member, send_messages=actor is bot_member))
        guild.get_channel_or_thread = lambda channel_id: channel
        context = register_requester(SimpleNamespace(
            private_pass=True, bot=SimpleNamespace(get_cog=lambda name: workflow),
            guild=guild, member=member, state=AgentTurnState()))
        with patch('elbow_helper.agent.features.rosters.signups.require_evidence_access',
                   new_callable=AsyncMock):
            arguments = {"roster_id": 17, "member_id": member.id, "accounts": ["Player"]}
            self.assertEqual((await prepare_roster_signup(context, arguments))["status"],
                             "confirmation_required")
            arguments["member_id"] = other.id
            with self.assertRaises(ActionRefused) as refusal:
                await prepare_roster_signup(context, arguments)
            self.assertEqual(refusal.exception.cause, "access_denied")
            member.roles.append(SimpleNamespace(id=CO_LEADER_ROLE_ID))
            self.assertEqual((await prepare_roster_signup(context, arguments))["status"],
                             "confirmation_required")
            own_signup, other_signup = context.state.proposed_changes
            self.assertTrue(await other_signup.preview.recheck())
            member.roles.clear()
            self.assertFalse(await other_signup.preview.recheck())
            self.assertTrue(await own_signup.preview.recheck())
