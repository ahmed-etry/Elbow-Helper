"""Action history and undo preparation stay tied to the requester."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.actions.contracts import ChangePreview, PreparedAction
from elbow_helper.features.agent.actions.log_tools import (
    read_agent_action_log, undo_agent_action,
)
from elbow_helper.features.agent.actions.targets import target_links


class ActionLogToolTests(unittest.IsolatedAsyncioTestCase):
    def test_command_target_links_exclude_quoted_values(self):
        links = target_links(1, {
            "channel": "5", "message_id": "6", "user": 7, "applicant": "8",
            "text": "Hidden quotation", "nickname": "9", "member_id": True,
            "thread_id": "A private thread name", "role_id": 10,
        })
        self.assertEqual(links, (
            "https://discord.com/channels/1/5", "https://discord.com/channels/1/5/6",
            "https://discord.com/users/7", "https://discord.com/users/8",
        ))

    async def test_read_filters_by_requester_and_pages(self):
        calls = []
        def recent_log(*, requester_id, offset, limit):
            calls.append((requester_id, offset, limit))
            return [{"log_id": "one", "action_name": "synthetic",
                     "action_label": "Change target", "action_class": "change",
                     "targets_json": '{"channel_id": 5, "message_id": 6, "text": "Synthetic private content"}',
                     "outcome": "completed",
                     "executed_at": 1000}]
        context = SimpleNamespace(
            member=SimpleNamespace(id=4),
            guild=SimpleNamespace(id=1),
            action_repository=SimpleNamespace(recent_log=recent_log),
        )
        with patch("elbow_helper.features.agent.actions.log_tools.require_evidence_access",
                   new_callable=AsyncMock):
            result = await read_agent_action_log(context, {"limit": 1})
        self.assertEqual(calls, [(4, 0, 2)])
        self.assertEqual(result["actions"][0]["target_links"], (
            "https://discord.com/channels/1/5", "https://discord.com/channels/1/5/6",
        ))
        self.assertNotIn("Synthetic private content", str(result))
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
            state=SimpleNamespace(proposed_changes=[]),
        )
        with patch("elbow_helper.features.agent.actions.log_tools.require_evidence_access",
                   new_callable=AsyncMock):
            result = await undo_agent_action(context, {"log_id": "one"})
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(context.state.proposed_changes, [action])
        run.assert_not_awaited()
