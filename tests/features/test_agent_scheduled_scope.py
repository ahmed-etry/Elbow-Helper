"""Scheduled actions stay within their confirmed values and target ceiling."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.scheduled.tools import prepare_manage, standing_tools
from elbow_helper.features.agent.actions.repository import AgentActionRepository

from elbow_helper.features.agent.actions.contracts import ChangePreview, PreparedAction
from elbow_helper.features.agent.scheduled.scope import validate_scope, within_scope


async def _unchanged():
    return True


async def _run():
    return None


def _action(values):
    return PreparedAction("add_discord_roles", values,
                          ChangePreview(("Change roles",), _unchanged), _run)


class ScheduledScopeTests(unittest.TestCase):
    async def test_run_between_manage_preview_and_confirmation_does_not_stale_the_preview(self):
        with TemporaryDirectory() as directory:
            repository = AgentActionRepository(Path(directory) / "actions.sqlite3")
            for operation in ("pause", "cancel"):
                identifier = repository.create_standing(kind="request", guild_id=1,
                    requester_id=2, destination_channel_id=3, rule={"request": "Synthetic"}, next_at=1)
                context = SimpleNamespace(action_repository=repository, member=SimpleNamespace(id=2),
                                          state=AgentTurnState())
                await prepare_manage(context, {"kind": "request", "operation": operation, "id": identifier})
                self.assertTrue(repository.claim_standing(kind="request", identifier=identifier,
                    version=0, owner="worker", now=2))
                self.assertTrue(repository.finish_standing(kind="request", identifier=identifier,
                    owner="worker", next_at=3))
                action = context.state.command_proposals[0]
                self.assertTrue(await action.preview.recheck())
                await action.run()
                self.assertFalse(await action.preview.recheck())

    def test_weekly_schema_uses_monday_zero_and_preserves_monthly_days(self):
        tool = next(tool for tool in standing_tools() if tool.definition.name == "save_standing_rule")
        schedule = tool.definition.parameters["properties"]["schedule"]
        weekly, monthly, _ = schedule["anyOf"]
        self.assertEqual(weekly["properties"]["days"]["items"], {"type": "integer", "minimum": 0, "maximum": 6})
        self.assertEqual(monthly["properties"]["days"]["items"]["maximum"], 31)

    def setUp(self):
        self.allowed = [{
            "capability": "add_discord_roles", "fixed_values": {"role_id": 12},
            "variable_fields": ["member_ids"], "max_targets": 2,
            "scope_text": "Add the chosen role to members",
        }]
        validate_scope(self.allowed)

    def test_fixed_values_and_target_limit(self):
        self.assertTrue(within_scope([_action({"role_id": 12, "member_ids": [1, 2]})],
                                     self.allowed))
        self.assertFalse(within_scope([_action({"role_id": 13, "member_ids": [1]})],
                                      self.allowed))
        self.assertFalse(within_scope([_action({"role_id": 12, "member_ids": [1, 2, 3]})],
                                      self.allowed))

    def test_new_options_and_multiple_actions_cannot_widen_scope(self):
        self.assertFalse(within_scope([_action({"role_id": 12, "member_ids": [1],
                                                "operation": "remove"})], self.allowed))
        self.assertFalse(within_scope([
            _action({"role_id": 12, "member_ids": [1, 2]}),
            _action({"role_id": 12, "member_ids": [3]}),
        ], self.allowed))

    def test_only_targets_and_message_content_may_change(self):
        with self.assertRaises(ValueError):
            validate_scope([{**self.allowed[0], "variable_fields": ["role_id"],
                             "fixed_values": {}}])
