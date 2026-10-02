"""Role actions preview each changed member and keep undo reversible."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.discord_actions.roles import (
    prepare_add_roles, prepare_remove_roles, prepare_role_undo,
)


class DiscordRoleActionTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        role = SimpleNamespace(
            id=3, mention="@role", position=1, managed=False,
            permissions=SimpleNamespace(), is_default=lambda: False,
        )
        bot_member = SimpleNamespace(id=1, top_role=SimpleNamespace(position=10))
        members = {}
        for member_id, roles in ((4, []), (5, [role])):
            member = SimpleNamespace(
                id=member_id, mention=f"@member{member_id}", roles=roles,
                top_role=SimpleNamespace(position=1),
            )
            async def add(selected_role, *, reason, member=member):
                member.roles.append(selected_role)
            async def remove(selected_role, *, reason, member=member):
                member.roles.remove(selected_role)
            member.add_roles = AsyncMock(side_effect=add)
            member.remove_roles = AsyncMock(side_effect=remove)
            members[member_id] = member
        async def fetch(member_id):
            return members[member_id]
        guild = SimpleNamespace(
            id=2, me=bot_member, get_role=lambda _: role,
            get_member=members.get, fetch_member=AsyncMock(side_effect=fetch),
        )
        context = SimpleNamespace(
            guild=guild, member=SimpleNamespace(id=8, display_name="Asker"),
            state=AgentTurnState(), roster_queries=None, role_connection_queries=None,
        )
        context.member.top_role = SimpleNamespace(position=5)
        context.member.guild_permissions = SimpleNamespace(manage_roles=True)
        original_get_member = guild.get_member
        guild.get_member = lambda identifier: context.member if identifier == context.member.id else original_get_member(identifier)
        return context, role, members

    async def test_bulk_role_change_prepares_only_members_who_need_it(self):
        context, role, members = self.context()
        with (patch("elbow_helper.features.agent.discord_actions.roles.require_evidence_access",
                    new_callable=AsyncMock),
              patch("elbow_helper.features.agent.discord_actions.roles.managed_role_commands",
                    new_callable=AsyncMock, return_value={})):
            result = await prepare_add_roles(
                context, {"role_id": role.id, "member_ids": [4, 5]},
            )
            self.assertEqual(result["prepared_count"], 1)
            action = context.state.proposed_changes[0]
            self.assertIn("@member4", action.preview.lines[0])
            self.assertTrue(await action.preview.recheck())
            outcome = await action.run()
            self.assertEqual(outcome.after, {"has_role": True})
            self.assertTrue(await action.verify())
            members[4].add_roles.assert_awaited_once()
            members[5].add_roles.assert_not_awaited()

    async def test_undo_reverses_the_recorded_change(self):
        context, role, members = self.context()
        with (patch("elbow_helper.features.agent.discord_actions.roles.require_evidence_access",
                    new_callable=AsyncMock),
              patch("elbow_helper.features.agent.discord_actions.roles.managed_role_commands",
                    new_callable=AsyncMock, return_value={})):
            await prepare_add_roles(context, {"role_id": role.id, "member_ids": [4]})
            action = context.state.proposed_changes[0]
            outcome = await action.run()
            undo = await prepare_role_undo(context, {
                "targets": action.values, "before": action.preview.before,
                "after": outcome.after,
            })
            self.assertTrue(await undo.preview.recheck())
            await undo.run()
            self.assertNotIn(role, members[4].roles)

    async def test_removal_previews_the_selected_member(self):
        context, role, members = self.context()
        with (patch("elbow_helper.features.agent.discord_actions.roles.require_evidence_access",
                    new_callable=AsyncMock),
              patch("elbow_helper.features.agent.discord_actions.roles.managed_role_commands",
                    new_callable=AsyncMock, return_value={})):
            result = await prepare_remove_roles(
                context, {"role_id": role.id, "member_ids": [4, 5]},
            )
            self.assertEqual(result["prepared_count"], 1)
            action = context.state.proposed_changes[0]
            self.assertIn("@member5", action.preview.lines[0])
            await action.run()
            self.assertNotIn(role, members[5].roles)
