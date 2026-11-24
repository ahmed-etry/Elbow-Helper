"""Panel roster changes use feature operations after a complete preview."""

from types import SimpleNamespace
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.actions.contracts import ActionClass
from elbow_helper.agent.engine.models import AgentTurnState
from elbow_helper.agent.features.rosters.management import roster_management_tools, prepare_roster_layout_undo
from elbow_helper.features.rosters.models import RosterLayout
from tests.agent.discord_actions.helpers import register_requester


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
        scope = SimpleNamespace(levels=frozenset({"lead_plus"}))
        requester = SimpleNamespace(id=8, roles=[SimpleNamespace(id=CO_LEADER_ROLE_ID)])
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda name: workflow),
            guild=guild, member=object(), state=AgentTurnState(),
        )
        context = register_requester(context)
        tool = next(tool for tool in roster_management_tools()
                    if tool.definition.name == "set_roster_layout")
        arguments = {"roster_id": 17, "show_clan": False}
        with (patch("elbow_helper.agent.features.rosters.management.require_evidence_access",
                    new_callable=AsyncMock),
              patch("elbow_helper.agent.engine.tool_call.require_evidence_access",
                    new_callable=AsyncMock),
              patch("elbow_helper.agent.safety.disclosure.request_scope",
                    new=AsyncMock(return_value=scope))):
            result = json.loads(await execute_tool(
                name=tool.definition.name, handler=tool.handler, arguments=arguments,
                capability_scope=compile_capability_call(tool, arguments),
                context=context, action_class=tool.action_class,
            ))
            self.assertEqual(result["status"], "confirmation_required")
            await prepare_preview(context)
        action = context.state.proposed_changes[0]
        self.assertIn("Clan: Visible to Hidden", action.preview.details)
        self.assertTrue(await action.preview.recheck())
        workflow.set_roster_layout.assert_not_awaited()
        result = await action.run()
        self.assertFalse(current.show_clan)
        undo = await prepare_roster_layout_undo(context, {
            "targets": action.values, "before": action.preview.before,
            "after": result.after,
        })
        self.assertTrue(await undo.preview.recheck())
        self.assertIn("Clan: Hidden to Visible", undo.preview.details)
        await undo.run()
        self.assertTrue(current.show_clan)

    async def test_show_and_hide_signup_controls_can_report_progress(self):
        # After Confirm the run shows the label's progress form; Show and Hide had none.
        from elbow_helper.agent.conversation.wording import action_progress_status
        for operation, hidden in (("show", True), ("hide", False)):
            with self.subTest(operation=operation):
                roster = SimpleNamespace(id=17, guild_id=1, name="War", status="open", buttons_hidden=hidden,
                                         active_cycle_id=3, role_id=None, reset_on_open=False)
                workflow = SimpleNamespace(roster_management_state=AsyncMock(return_value={
                    "roster": roster, "account_count": 0, "member_ids": (), "posts": ()}))
                guild = SimpleNamespace(id=1, me=object())
                context = register_requester(SimpleNamespace(private_pass=True,
                    bot=SimpleNamespace(get_cog=lambda name: workflow),
                    guild=guild, member=object(), state=AgentTurnState()))
                tool = next(tool for tool in roster_management_tools()
                            if tool.definition.name == "set_roster_state")
                with patch('elbow_helper.agent.features.rosters.management.require_evidence_access',
                           new_callable=AsyncMock):
                    result = await tool.handler(context, {"roster_id": 17, "operation": operation})
                self.assertEqual(result["status"], "confirmation_required")
                summary = context.state.proposed_changes[0].preview.summary
                self.assertTrue(action_progress_status(summary).endswith(" roster signup controls"))

    async def test_a_member_who_left_does_not_block_clear_refresh_or_reset_open(self):
        # One of 598 signups belonged to someone no longer in the server; each path answered "unavailable".
        import discord
        for tool_name, arguments in (("clear_roster_signups", {"roster_id": 17}),
                                     ("refresh_roster", {"roster_id": 17}),
                                     ("set_roster_state", {"roster_id": 17, "operation": "open"})):
            with self.subTest(tool=tool_name):
                roster = SimpleNamespace(id=17, guild_id=1, name="War", status="closed", buttons_hidden=False,
                                         active_cycle_id=3, role_id=8, reset_on_open=True)
                snapshot = {"roster": roster, "account_count": 2, "member_ids": (41, 43), "posts": ()}
                workflow = SimpleNamespace(roster_management_state=AsyncMock(return_value=snapshot))
                bot_member = SimpleNamespace(id=999, top_role=SimpleNamespace(position=10))
                role = SimpleNamespace(id=8, mention="<@&8>", managed=False, position=1,
                                       permissions=SimpleNamespace(), is_default=lambda: False)
                present = SimpleNamespace(id=41, mention="<@41>", top_role=SimpleNamespace(position=1))

                async def fetch_member(member_id):
                    raise discord.NotFound(SimpleNamespace(status=404, reason="Unknown Member"), "gone")

                guild = SimpleNamespace(id=1, me=bot_member, get_role=lambda role_id: role,
                                        get_member=lambda member_id: present if member_id == 41 else None,
                                        fetch_member=fetch_member)
                context = register_requester(SimpleNamespace(private_pass=True,
                    bot=SimpleNamespace(get_cog=lambda name: workflow),
                    guild=guild, member=object(), state=AgentTurnState()))
                tool = next(tool for tool in roster_management_tools() if tool.definition.name == tool_name)
                with patch('elbow_helper.agent.features.rosters.management.require_evidence_access',
                           new_callable=AsyncMock):
                    result = await tool.handler(context, arguments)
                self.assertEqual(result["status"], "confirmation_required")
                lines = context.state.proposed_changes[0].preview.lines
                self.assertFalse(any("<@43>" in line for line in lines))
                self.assertTrue(await context.state.proposed_changes[0].preview.recheck())

    async def test_bulk_add_skips_checks_for_an_owner_who_left(self):
        import discord
        from elbow_helper.agent.features.rosters.signups import prepare_bulk_roster_add
        roster = SimpleNamespace(id=17, guild_id=1, name="War", role_id=None)
        workflow = SimpleNamespace(bulk_add_roster_preview=AsyncMock(return_value={
            "roster": roster, "posts": (),
            "accounts": (("#P0", "Synthetic", 41, False), ("#Q0", "Departed", 43, True))}))

        async def fetch_member(member_id):
            raise discord.NotFound(SimpleNamespace(status=404, reason="Unknown Member"), "gone")

        present = SimpleNamespace(id=41, mention="<@41>", top_role=SimpleNamespace(position=1))
        guild = SimpleNamespace(id=1, me=SimpleNamespace(id=999, top_role=SimpleNamespace(position=10)),
                                get_role=lambda role_id: None, fetch_member=fetch_member,
                                get_member=lambda member_id: present if member_id == 41 else None)
        context = register_requester(SimpleNamespace(private_pass=True,
            bot=SimpleNamespace(get_cog=lambda name: workflow),
            guild=guild, member=object(), state=AgentTurnState()))
        with patch('elbow_helper.agent.features.rosters.signups.require_evidence_access', new_callable=AsyncMock):
            result = await prepare_bulk_roster_add(context, {"roster_id": 17, "player_tags": ["#P0", "#Q0"]})
        self.assertEqual(result["status"], "confirmation_required")
        self.assertTrue(any("<@43>" in line for line in context.state.proposed_changes[0].preview.lines))

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
        context = SimpleNamespace(private_pass=True,
            bot=SimpleNamespace(get_cog=lambda name: workflow),
            guild=guild, member=object(), state=AgentTurnState(),
        )
        context = register_requester(context)
        tool = next(tool for tool in roster_management_tools()
                    if tool.definition.name == "clear_roster_signups")
        with patch('elbow_helper.agent.features.rosters.management.require_evidence_access',
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
