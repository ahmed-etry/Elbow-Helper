"""Scheduled actions stay within their confirmed values and target ceiling."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.scheduled.tools import prepare_manage, standing_tools
from elbow_helper.features.agent.actions.repository import AgentActionRepository
import json
from types import SimpleNamespace

from elbow_helper.features.agent.actions.contracts import ChangePreview, PreparedAction
from elbow_helper.features.agent.scheduled.scope import validate_scope, within_scope
from elbow_helper.features.agent.scheduled.tools import _field_label, _fixed_display
from elbow_helper.features.agent.scheduled.tools import _watcher_reads
from elbow_helper.features.agent.engine.capability_contract import CapabilityContract
from elbow_helper.features.agent.models import RegisteredAgentTool, AgentCapabilityEffect
from elbow_helper.infrastructure.ai.agent import AgentToolDefinition
from unittest.mock import patch


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
        tool = next(tool for tool in standing_tools(lambda: {}) if tool.definition.name == "save_standing_rule")
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

    def test_command_scope_uses_the_shared_feature_path(self):
        allowed = [{**self.allowed[0], "capability": "run_command_role_grant",
                    "action_path": "/role grant"}]
        action = PreparedAction("/role grant", {"role_id": 12, "member_ids": [1]},
                                ChangePreview(("Grant role",), _unchanged), _run)
        self.assertTrue(within_scope((action,), allowed))

    def test_fixed_discord_values_render_as_mentions(self):
        self.assertEqual(_field_label("role_id"), "role")
        self.assertEqual(_field_label("member_ids"), "members")
        guild = SimpleNamespace(
            get_role=lambda _: SimpleNamespace(mention="<@&12>"),
            get_member=lambda _: SimpleNamespace(mention="<@21>"),
            get_channel_or_thread=lambda _: SimpleNamespace(mention="<#3>"),
        )
        context = SimpleNamespace(guild=guild)
        self.assertEqual(_fixed_display(context, "role_id", 12), "<@&12>")
        self.assertEqual(_fixed_display(context, "member_ids", [21]), "<@21>")
        self.assertEqual(_fixed_display(context, "channel_id", 3), "<#3>")
        with self.assertRaises(ValueError):
            _fixed_display(context, "unresolved_id", 45)

    def test_separate_fixed_scopes_keep_their_own_target_limits(self):
        allowed = [self.allowed[0], {
            **self.allowed[0], "fixed_values": {"role_id": 13}, "max_targets": 1,
        }]
        self.assertTrue(within_scope([
            _action({"role_id": 12, "member_ids": [1, 2]}),
            _action({"role_id": 13, "member_ids": [3]}),
        ], allowed))
        self.assertFalse(within_scope([
            _action({"role_id": 12, "member_ids": [1, 2]}),
            _action({"role_id": 13, "member_ids": [3, 4]}),
        ], allowed))

    def test_duplicate_scope_cannot_double_a_target_limit(self):
        with self.assertRaises(ValueError):
            validate_scope([self.allowed[0], dict(self.allowed[0])])

    def test_scope_text_rejects_raw_discord_ids(self):
        with self.assertRaises(ValueError):
            validate_scope([{**self.allowed[0],
                             "scope_text": "Grant role 123456789012345678"}])
        validate_scope([{**self.allowed[0],
                         "scope_text": "Grant <@&123456789012345678> in <#123456789012345679>"}])

    def test_feature_targets_use_names_from_checked_evidence(self):
        context = SimpleNamespace(state=SimpleNamespace(evidence=[json.dumps({
            "result": json.dumps({"items": [{"roster_id": 7, "name": "War roster"}]})
        })]))
        self.assertEqual(_fixed_display(context, "roster_id", 7), "War roster")
        self.assertEqual(_fixed_display(context, "roster", "7"), "War roster")

    def test_watcher_rejects_retained_and_historical_reads(self):
        tool = RegisteredAgentTool(AgentToolDefinition(
            "synthetic", "Read state", {"type": "object", "properties": {}}), _run)
        reads = [{"capability": "synthetic", "arguments": {}}]
        for contract in (
            CapabilityContract((), (), retained_fields=("report_id",)),
            CapabilityContract((), (), time_window=("after", "before", "iso_utc")),
        ):
            with patch("elbow_helper.features.agent.engine.registry.build_agent_tools",
                       return_value={"synthetic": tool}), patch(
                       "elbow_helper.features.agent.engine.capability_contract.CONTRACTS",
                       {"synthetic": contract}), self.assertRaises(ValueError):
                _watcher_reads(reads)

    def test_watcher_accepts_current_reads_and_rejects_state_changes(self):
        definition = AgentToolDefinition(
            "synthetic", "Read state", {"type": "object", "properties": {}})
        reads = [{"capability": "synthetic", "arguments": {}}]
        for effect in (AgentCapabilityEffect.READ, AgentCapabilityEffect.STATE):
            with patch("elbow_helper.features.agent.engine.registry.build_agent_tools",
                       return_value={"synthetic": RegisteredAgentTool(definition, _run, effect)}), patch(
                       "elbow_helper.features.agent.engine.capability_contract.CONTRACTS",
                       {"synthetic": CapabilityContract((), ())}):
                if effect is AgentCapabilityEffect.READ:
                    _watcher_reads(reads)
                else:
                    with self.assertRaises(ValueError):
                        _watcher_reads(reads)

    def test_target_strings_cannot_bypass_the_target_limit(self):
        allowed = [{**self.allowed[0], "variable_fields": ["players"]}]
        self.assertTrue(within_scope([_action({"role_id": 12,
                                               "players": "<@1><@2>"})], allowed))
        self.assertFalse(within_scope([_action({"role_id": 12,
                                                "players": "<@1>, <@2> <@3>"})], allowed))
