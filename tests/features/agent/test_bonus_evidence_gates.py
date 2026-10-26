"""Public CWL scores do not authorize retained bonus-review source text."""

from dataclasses import replace
import json
from tempfile import TemporaryDirectory
import unittest

from elbow_helper.configuration import roles
from elbow_helper.features.cwl.config import CWL_HQ_CHANNEL_ID
from elbow_helper.features.agent.access import AgentAccessLost, require_evidence_access
from elbow_helper.features.agent.capabilities.cwl.data import SOURCES
from elbow_helper.features.agent.datasets.query import query_context, run_query
from features.agent.dataset_helpers import synthetic_datasets
from features.agent import test_evidence_scope as fixtures


class BonusEvidenceGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = fixtures.ScopeTests()
        fixture.setUp()
        self.context = replace(fixture.context, private_pass=True)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.context.bot.paths = synthetic_datasets(directory.name)
        self.source = fixture.personal
        self.hq = fixtures.Channel(CWL_HQ_CHANNEL_ID, fixture.guild, {10, 11, 99}, level=roles.CWL_HELPERS)
        fixture.guild.channels.append(self.hq)
        self.data_source = next(source for source in SOURCES if source.state_name == "cwl_bonus_dashboard")
        self.data_source.resolve(self.context.bot.paths).write_text(json.dumps({"boards": {"review:1": {
            "channel_id": self.hq.id, "clans": {"BE1": {"source_channel_id": self.source.id,
            "source_url": f"https://discord.com/channels/1/{self.source.id}/123",
            "source_text": "Restricted review evidence", "recipient_ids": [10]}}}}}), encoding="utf-8")
        self.sql = "SELECT doc FROM state.cwl_bonus_dashboard"

    async def test_each_operator_role_can_read_in_suitable_scope(self):
        for role in roles.LEAD_PLUS | roles.CWL_HELPERS | {roles.MEMBER_ROLE_ID}:
            with self.subTest(role=role):
                self.context.member.roles = [fixtures.Role(roles.AGENT_TESTER_ROLE_ID), fixtures.Role(role)]
                self.context = replace(self.context, state=type(self.context.state)())
                result = await query_context(self.context, self.sql)
                self.assertEqual("error" not in result, role != roles.MEMBER_ROLE_ID)
                if "error" not in result:
                    self.assertIn(self.source.id, self.context.state.source_channels)
                    self.assertIn(self.hq.id, self.context.state.source_channels)

    async def test_source_audience_blocks_shared_result_and_revocation_blocks_reuse(self):
        cwl_role = fixtures.Role(next(iter(roles.CWL_HELPERS)))
        self.context.member.roles.append(cwl_role)
        self.context.guild.get_member(11).roles.append(cwl_role)
        shared = replace(self.context, private_pass=False)
        shared.source_message.channel = self.hq
        result = await query_context(shared, self.sql)
        self.assertIn("error", result)
        self.assertNotIn("Restricted review evidence", str(result))
        self.context = replace(self.context, state=type(self.context.state)())
        result = await query_context(self.context, self.sql)
        self.assertNotIn("error", result)
        self.source.viewers.remove(10)
        with self.assertRaises(AgentAccessLost):
            await require_evidence_access(self.context)

    def test_worker_cannot_read_review_doc_without_all_source_authorizations(self):
        result, _ = run_query(SOURCES, self.context.bot.paths, {"cwl"}, self.sql,
                              channel_ids={self.hq.id})
        self.assertIn("error", result)
        self.assertNotIn("Restricted review evidence", str(result))
        result, _ = run_query(SOURCES, self.context.bot.paths, set(),
                              "SELECT doc FROM state.cwl_bonus_config")
        self.assertNotIn("error", result)
