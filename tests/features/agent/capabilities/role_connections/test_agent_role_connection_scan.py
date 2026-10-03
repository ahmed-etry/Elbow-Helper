"""Role connection scans preview guarded member changes individually."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.capabilities.role_connections.scan import (
    prepare_role_connection_scan, prepare_role_connection_scan_undo,
)


class RoleConnectionScanTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_member_change_is_confirmed_and_can_be_undone(self):
        role = SimpleNamespace(
            id=8, mention="<@&8>", position=2, managed=False,
            permissions=SimpleNamespace(), is_default=lambda: False,
        )
        member = SimpleNamespace(
            id=4, mention="<@4>", top_role=SimpleNamespace(position=1), roles=[],
        )
        bot_member = SimpleNamespace(id=99, top_role=SimpleNamespace(position=10))

        async def apply(target, selected_role, *, add):
            target.roles = [selected_role] if add else []
            return True

        workflow = SimpleNamespace(
            connections_board_signature=lambda: "signature",
            role_connection_scan_plan=AsyncMock(return_value=((member, ((role, True),)),)),
            apply_role_connection_change=AsyncMock(side_effect=apply),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda name: workflow),
            guild=SimpleNamespace(id=1, me=bot_member, get_role=lambda role_id: role),
            state=AgentTurnState(),
        )
        with patch("elbow_helper.features.agent.capabilities.role_connections.scan.require_evidence_access",
                   new_callable=AsyncMock):
            result = await prepare_role_connection_scan(context, {})
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(len(context.state.proposed_changes), 1)
        action = context.state.proposed_changes[0]
        self.assertIn("<@4>", action.preview.lines[0])
        workflow.apply_role_connection_change.assert_not_awaited()
        with patch("elbow_helper.features.agent.capabilities.role_connections.scan.resolve_member",
                   new_callable=AsyncMock, return_value=member):
            self.assertTrue(await action.preview.recheck())
            outcome = await action.run()
            undo = await prepare_role_connection_scan_undo(context, {
                "targets": action.values, "before": action.preview.before,
                "after": outcome.after,
            })
            self.assertTrue(await undo.preview.recheck())
            await undo.run()
        self.assertEqual(member.roles, [])

    async def test_feature_scan_allows_role_at_bot_hierarchy(self):
        role = SimpleNamespace(
            id=8, mention="<@&8>", position=10, managed=False,
            permissions=SimpleNamespace(), is_default=lambda: False,
        )
        member = SimpleNamespace(id=4, mention="<@4>", top_role=SimpleNamespace(position=1), roles=[])
        workflow = SimpleNamespace(
            connections_board_signature=lambda: "signature",
            role_connection_scan_plan=AsyncMock(return_value=((member, ((role, True),)),)),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda name: workflow),
            guild=SimpleNamespace(id=1, me=SimpleNamespace(
                id=99, top_role=SimpleNamespace(position=10))),
            state=AgentTurnState(),
        )
        with patch("elbow_helper.features.agent.capabilities.role_connections.scan.require_evidence_access",
                   new_callable=AsyncMock):
            result = await prepare_role_connection_scan(context, {})
            self.assertEqual(result["status"], "confirmation_required")
