"""Action history and undo preparation stay tied to the requester."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.actions.contracts import ChangePreview, PreparedAction
from elbow_helper.features.agent.actions.log_tools import (
    read_agent_action_log, undo_agent_action,
)


class ActionLogToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_filters_by_requester_and_pages(self):
        calls = []
        def recent_log(*, requester_id, offset, limit):
            calls.append((requester_id, offset, limit))
            return [{"log_id": "one", "action_name": "synthetic",
                     "action_label": "Change target", "action_class": "change",
                     "targets_json": '{"target": 5}', "outcome": "completed",
                     "executed_at": 1000}]
        context = SimpleNamespace(
            member=SimpleNamespace(id=4),
            action_repository=SimpleNamespace(recent_log=recent_log),
        )
        with patch("elbow_helper.features.agent.actions.log_tools.require_evidence_access",
                   new_callable=AsyncMock):
            result = await read_agent_action_log(context, {"limit": 1})
        self.assertEqual(calls, [(4, 0, 2)])
        self.assertEqual(result["actions"][0]["targets"], {"target": 5})
        self.assertEqual(result["actions"][0]["log_id"], "one")

    async def test_undo_only_prepares_a_change(self):
        run = AsyncMock()
        action = PreparedAction(
            "synthetic_undo", {"target": 5},
            ChangePreview(("Restore target 5",), AsyncMock(return_value=True)),
            run,
        )
        runner = SimpleNamespace(prepare_undo=AsyncMock(return_value=action))
        context = SimpleNamespace(
            action_runner=runner,
            state=SimpleNamespace(command_proposals=[]),
        )
        with patch("elbow_helper.features.agent.actions.log_tools.require_evidence_access",
                   new_callable=AsyncMock):
            result = await undo_agent_action(context, {"log_id": "one"})
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(context.state.command_proposals, [action])
        run.assert_not_awaited()
