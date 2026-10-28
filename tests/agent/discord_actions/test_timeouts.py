"""Synthetic timeout confirmation, hierarchy, limits and undo checks."""

import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.agent.discord_actions.timeouts import prepare_timeout, undo_timeout
from agent.discord_actions.management_helpers import fixture


class TimeoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        access = patch("elbow_helper.agent.discord_actions.management."
                       "require_evidence_access", new_callable=AsyncMock)
        access.start()
        self.addCleanup(access.stop)

    def context(self):
        context, channel, role, member = fixture()
        member.communication_disabled_until = None

        async def timeout(until, **kwargs):
            member.communication_disabled_until = until

        member.timeout = AsyncMock(side_effect=timeout)
        return context, channel, role, member

    async def test_timeout_waits_for_confirm_and_undo_removes_it(self):
        context, _, _, member = self.context()
        await prepare_timeout(context, {"member_id": member.id, "operation": "set",
                                        "duration_seconds": 600, "reason": "Synthetic reason"})
        member.timeout.assert_not_called()
        action = context.state.proposed_changes[0]
        self.assertIn("<t:", action.preview.lines[0])
        result = await action.run()
        self.assertIsNotNone(member.communication_disabled_until)
        undo = await undo_timeout(context, {
            "targets": action.values, "before": action.preview.before, "after": result.after,
        })
        await undo.run()
        self.assertIsNone(member.communication_disabled_until)

    async def test_limits_hierarchy_administrator_and_bot_permission_refuse_previews(self):
        for kind in ("limit", "hierarchy", "administrator", "bot"):
            context, _, role, member = self.context()
            arguments = {"member_id": member.id, "operation": "set", "duration_seconds": 60}
            if kind == "limit":
                arguments["duration_seconds"] = 28 * 86400 + 1
            elif kind == "hierarchy":
                role.position = context.member.top_role.position
            elif kind == "administrator":
                member.guild_permissions = discord.Permissions(administrator=True)
            else:
                context.guild.me.guild_permissions = discord.Permissions.none()
            with self.assertRaises(ValueError):
                await prepare_timeout(context, arguments)
            self.assertFalse(context.state.proposed_changes)
            member.timeout.assert_not_called()

    async def test_permission_loss_is_rechecked_at_execution(self):
        context, _, _, member = self.context()
        await prepare_timeout(context, {"member_id": member.id, "operation": "set",
                                        "duration_seconds": 60})
        context.member.guild_permissions = discord.Permissions.none()
        with self.assertRaises(ValueError):
            await context.state.proposed_changes[0].run()
        member.timeout.assert_not_called()

    async def test_expired_deadline_is_refused_at_confirmation(self):
        context, _, _, member = self.context()
        await prepare_timeout(context, {"member_id": member.id, "operation": "set",
                                        "duration_seconds": 60})
        from datetime import datetime, timedelta, timezone
        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        with patch("elbow_helper.agent.discord_actions.timeouts.datetime") as clock:
            clock.now.return_value = future
            with self.assertRaisesRegex(ValueError, "A timeout must be between"):
                await context.state.proposed_changes[0].run()
        member.timeout.assert_not_called()

    async def test_combined_audit_reason_limit_is_checked_before_preview(self):
        context, _, _, member = self.context()
        with patch("elbow_helper.agent.discord_actions.timeouts.audit_reason",
                   return_value="S" * 120):
            with self.assertRaisesRegex(ValueError, "The selected reason is invalid"):
                await prepare_timeout(context, {"member_id": member.id, "operation": "set",
                                                "duration_seconds": 60, "reason": "S" * 400})
        self.assertFalse(context.state.proposed_changes)
        member.timeout.assert_not_called()
