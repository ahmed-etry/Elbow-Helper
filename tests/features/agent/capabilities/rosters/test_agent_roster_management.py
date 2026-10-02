"""Panel roster changes use feature operations after a complete preview."""

from types import SimpleNamespace
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.capabilities.rosters.management import (
    roster_management_tools, prepare_roster_layout_undo,
)
from elbow_helper.features.rosters.models import RosterLayout


class RosterManagementActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_layout_preview_and_undo_restore_prior_columns(self):
        roster = SimpleNamespace(id=17, guild_id=1, name="War")
        current = RosterLayout()

        async def state(roster_id):
            return roster, current

        async def change(roster_id, **values):
            nonlocal current
            current = replace(current, **values)
            return roster, current

        workflow = SimpleNamespace(
            roster_layout_state=AsyncMock(side_effect=state),
            roster_edit_state=AsyncMock(return_value={"posts": ((9, 91),)}),
            set_roster_layout=AsyncMock(side_effect=change),
        )
        guild = SimpleNamespace(id=1, me=object())
        channel = SimpleNamespace(guild=guild, permissions_for=lambda actor: SimpleNamespace(
            view_channel=True, send_messages=True))
        guild.get_channel_or_thread = lambda channel_id: channel
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda name: workflow),
            guild=guild, member=object(), state=AgentTurnState(),
        )
        tool = next(tool for tool in roster_management_tools()
                    if tool.definition.name == "set_roster_layout")
        with patch("elbow_helper.features.agent.capabilities.rosters.management.require_evidence_access",
                   new_callable=AsyncMock):
            await tool.handler(context, {"roster_id": 17, "show_clan": False})
        action = context.state.proposed_changes[0]
        self.assertTrue(any("Show Clan: True to False" in line for line in action.preview.details))
        self.assertTrue(await action.preview.recheck())
        workflow.set_roster_layout.assert_not_awaited()
        result = await action.run()
        self.assertFalse(current.show_clan)
        undo = await prepare_roster_layout_undo(context, {
            "targets": action.values, "before": action.preview.before,
            "after": result.after,
        })
        self.assertTrue(await undo.preview.recheck())
        await undo.run()
        self.assertTrue(current.show_clan)

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
        bot_member = SimpleNamespace(id=999, top_role=SimpleNamespace(position=10))
        role = SimpleNamespace(id=8, mention="<@&8>", managed=False, position=1,
                               permissions=SimpleNamespace(), is_default=lambda: False)
        guild = SimpleNamespace(id=1, me=bot_member, get_role=lambda role_id: role,
                                get_member=lambda member_id: SimpleNamespace(
                                    id=member_id, top_role=SimpleNamespace(position=1)))
        channel = SimpleNamespace(guild=guild, permissions_for=lambda actor: SimpleNamespace(
            view_channel=True, send_messages=True))
        guild.get_channel_or_thread = lambda channel_id: channel
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda name: workflow),
            guild=guild, member=object(), state=AgentTurnState(),
        )
        tool = next(tool for tool in roster_management_tools()
                    if tool.definition.name == "clear_roster_signups")
        with patch("elbow_helper.features.agent.capabilities.rosters.management.require_evidence_access",
                   new_callable=AsyncMock):
            result = await tool.handler(context, {"roster_id": 17})
        self.assertEqual(result["status"], "confirmation_required")
        action = context.state.proposed_changes[0]
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
