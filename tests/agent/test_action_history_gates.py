"""Durable history rechecks its original sources and current destination."""

from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock

from elbow_helper.configuration import roles
from elbow_helper.agent.access import AgentAccessLost, require_evidence_access
from elbow_helper.agent.actions.contracts import ChangePreview, PreparedAction
from elbow_helper.agent.actions.log_tools import read_agent_action_log
from elbow_helper.agent.actions.runner import _history_preview
from elbow_helper.agent.actions.store import AgentActionRepository
from elbow_helper.agent.models import AgentTurnState
from agent import test_evidence_scope as fixtures


class ActionHistoryGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = fixtures.ScopeTests()
        fixture.setUp()
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repository = AgentActionRepository(Path(directory.name) / "actions.db")
        self.context = replace(fixture.context, private_pass=True,
                               action_repository=self.repository)
        self.context.member.roles.append(fixtures.Role(next(iter(roles.LEAD))))
        self.source = fixtures.Channel(301, self.context.guild, {10, 99}, level=roles.LEAD)
        self.context.guild.channels.append(self.source)

    def record(self, *, sources=(301,), access=("lead",), targets=None,
               guild_id=1, requester_id=10, preview=None):
        if preview is None:
            preview = {"lines": ["Secret review"], "detail_sources": list(sources),
                       "detail_access": list(access)}
        run_id = self.repository.create_run(
            guild_id=guild_id, channel_id=100, request_message_id=1000,
            requester_id=requester_id, confirmer_id=requester_id,
            steps=({"name": "private_review", "label": "Secret member review",
                    "class": "change", "values": targets or {"channel_id": 301, "message_id": 9},
                    "preview": preview},),
        )
        self.repository.claim(run_id, owner="test")
        self.repository.start_step(run_id, 0, owner="test")
        return run_id

    async def test_durable_provenance_and_retained_revocation(self):
        self.record()
        result = await read_agent_action_log(self.context, {})
        self.assertTrue(result["actions"][0]["details_available"])
        self.assertIn("301/9", str(result))
        self.assertEqual(self.context.state.required_access, {"lead"})
        self.assertIn(301, self.context.state.source_channels)
        self.context.member.roles = [fixtures.Role(roles.AGENT_TESTER_ROLE_ID)]
        with self.assertRaises(AgentAccessLost):
            await require_evidence_access(self.context)

    async def test_role_loss_hides_labels_and_links_even_for_owner(self):
        self.record()
        self.context.member.roles = [fixtures.Role(roles.AGENT_TESTER_ROLE_ID)]
        result = await read_agent_action_log(self.context, {})
        self.assertFalse(result["actions"][0]["details_available"])
        self.assertNotIn("Secret member review", json.dumps(result))
        self.assertNotIn("private_review", json.dumps(result))
        self.assertEqual(result["actions"][0]["target_links"], ())

    async def test_source_target_bot_and_destination_access_are_rechecked(self):
        for change in ("source", "target", "bot", "destination"):
            with self.subTest(change=change):
                self.context = replace(self.context, private_pass=True, state=AgentTurnState())
                self.context.source_message.channel = self.context.guild.channels[0]
                self.source.viewers = {10, 99}
                run_id = self.record(
                    sources=() if change == "target" else (301,),
                    access=() if change == "target" else ("lead",),
                )
                if change in ("source", "target"):
                    self.source.overwrites = {}
                    self.source.viewers.remove(10)
                elif change == "bot":
                    self.source.viewers.remove(99)
                else:
                    self.context = replace(self.context, private_pass=False)
                    self.context.source_message.channel = self.context.guild.channels[1]
                result = await read_agent_action_log(self.context, {"limit": 1})
                self.assertFalse(result["actions"][0]["details_available"])
                self.assertEqual(result["actions"][0]["target_links"], ())
                self.assertFalse(self.context.state.source_channels)
                with self.repository.connect() as conn:
                    conn.execute("DELETE FROM action_log WHERE run_id=?", (run_id,))

    async def test_legacy_corrupt_foreign_records_are_redacted_and_owner_filter_pages(self):
        self.record(requester_id=11)
        for kwargs in ({"preview": ["Legacy secret"]},
                       {"preview": {"detail_sources": "invalid", "detail_access": []}},
                       {"guild_id": 2}):
            self.record(**kwargs)
        result = await read_agent_action_log(self.context, {"limit": 2})
        self.assertEqual(len(result["actions"]), 2)
        self.assertEqual(result["next_offset"], 2)
        self.assertTrue(all(not row["details_available"] for row in result["actions"]))
        page = await read_agent_action_log(self.context, {"offset": 2, "limit": 2})
        self.assertEqual(len(page["actions"]), 1)
        self.assertIsNone(page["next_offset"])

    def test_bound_preview_retains_original_and_new_provenance(self):
        def action(sources, access):
            return PreparedAction("change", {}, ChangePreview(
                ("Change",), AsyncMock(return_value=True),
                detail_sources=frozenset(sources), detail_access=frozenset(access),
            ), AsyncMock())
        preview = _history_preview(self.context, action({302}, {"core"}),
                                   original=action({301}, {"lead"}))
        self.assertEqual(preview["detail_sources"], [301, 302])
        self.assertEqual(preview["detail_access"], ["core", "lead"])
