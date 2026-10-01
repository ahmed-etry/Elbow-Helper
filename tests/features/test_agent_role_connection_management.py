"""Role connection panel changes preview the complete intended rule."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.tools.role_connection_management import (
    prepare_role_connection_change, prepare_remove_role_connection,
    prepare_role_connection_undo,
)


class _Channel:
    id = 9
    mention = "<#9>"


class RoleConnectionManagementTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_waits_for_confirm_and_posts_board(self):
        role = SimpleNamespace(mention="<@&8>")
        workflow = SimpleNamespace(
            new_connection_id=lambda: "rule-id",
            connection_change_is_valid=lambda *args, **kwargs: True,
            role_connection_state=lambda conn_id: None,
            add_connection=MagicMock(),
            refresh_connections_message=AsyncMock(return_value=SimpleNamespace(jump_url="board-url")),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda name: workflow),
            guild=SimpleNamespace(get_role=lambda role_id: role, me=object()),
            member=object(), source_message=SimpleNamespace(channel=SimpleNamespace(id=9)),
            state=AgentTurnState(),
        )
        with (patch("elbow_helper.features.agent.tools.role_connection_management.require_evidence_access",
                    new_callable=AsyncMock),
              patch("elbow_helper.features.agent.tools.role_connection_management.resolve_channel",
                    new_callable=AsyncMock, return_value=_Channel()),
              patch("elbow_helper.features.agent.tools.role_connection_management.check_post_access"),
              patch("elbow_helper.features.agent.tools.role_connection_management.check_role"),
              patch("elbow_helper.features.agent.tools.role_connection_management.discord.TextChannel",
                    _Channel)):
            result = await prepare_role_connection_change(context, {
                "operation": "create", "target_role_id": 8,
                "all": [{"has": 7}],
            })
        self.assertEqual(result["status"], "confirmation_required")
        action = context.state.command_proposals[0]
        self.assertTrue(any("<@&8>" in line for line in action.preview.lines))
        workflow.add_connection.assert_not_called()
        with patch("elbow_helper.features.agent.tools.role_connection_management.check_post_access"), \
             patch("elbow_helper.features.agent.tools.role_connection_management.check_role"):
            self.assertTrue(await action.preview.recheck())
        outcome = await action.run()
        self.assertIn("board-url", outcome.text)
        workflow.add_connection.assert_called_once()
        workflow.refresh_connections_message.assert_awaited_once()
        workflow.role_connection_state = lambda conn_id: outcome.after["connection"]
        workflow.remove_connection = MagicMock(return_value=True)
        with (patch("elbow_helper.features.agent.tools.role_connection_management.resolve_channel",
                    new_callable=AsyncMock, return_value=_Channel()),
              patch("elbow_helper.features.agent.tools.role_connection_management.check_post_access"),
              patch("elbow_helper.features.agent.tools.role_connection_management.check_role"),
              patch("elbow_helper.features.agent.tools.role_connection_management.discord.TextChannel",
                    _Channel)):
            undo = await prepare_role_connection_undo(context, {
                "targets": action.values, "before": action.preview.before,
                "after": outcome.after,
            })
        self.assertTrue(await undo.preview.recheck())
        await undo.run()
        workflow.remove_connection.assert_called_once_with("rule-id")

    async def test_removal_is_irreversible(self):
        from elbow_helper.features.agent.actions.contracts import ActionClass
        role = SimpleNamespace(mention="<@&8>")
        existing = {"id": "rule-id", "target_role_id": 8,
                    "all": [{"has": 7}], "any": []}
        workflow = SimpleNamespace(
            role_connection_state=lambda conn_id: existing,
            remove_connection=MagicMock(return_value=True),
            refresh_connections_message=AsyncMock(return_value=SimpleNamespace(jump_url="board-url")),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda name: workflow),
            guild=SimpleNamespace(get_role=lambda role_id: role, me=object()),
            member=object(), source_message=SimpleNamespace(channel=SimpleNamespace(id=9)),
            state=AgentTurnState(),
        )
        with (patch("elbow_helper.features.agent.tools.role_connection_management.require_evidence_access",
                    new_callable=AsyncMock),
              patch("elbow_helper.features.agent.tools.role_connection_management.resolve_channel",
                    new_callable=AsyncMock, return_value=_Channel()),
              patch("elbow_helper.features.agent.tools.role_connection_management.check_post_access"),
              patch("elbow_helper.features.agent.tools.role_connection_management.check_role"),
              patch("elbow_helper.features.agent.tools.role_connection_management.discord.TextChannel",
                    _Channel)):
            await prepare_remove_role_connection(context, {"connection_id": "rule-id"})
        self.assertIs(context.state.command_proposals[0].action_class, ActionClass.IRREVERSIBLE)
