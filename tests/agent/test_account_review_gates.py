"""Unverified account ownership stays with account reviewers."""

from dataclasses import replace
from tempfile import TemporaryDirectory
import unittest

from elbow_helper.configuration import roles
from elbow_helper.agent.access import AgentAccessLost, require_evidence_access
from elbow_helper.agent.datasets.query import query_context
from agent.dataset_helpers import synthetic_datasets
from agent import test_evidence_scope as fixtures


class AccountReviewGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = fixtures.ScopeTests()
        fixture.setUp()
        self.context = replace(fixture.context, private_pass=True)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.context.bot.paths = synthetic_datasets(directory.name)

    async def test_review_tables_require_actual_reviewer_roles(self):
        for role in (roles.MEMBER_ROLE_ID, *roles.LEAD_PLUS, *roles.RECRUITERS, *roles.CORE):
            for table in ("suggestions", "ignored_tags"):
                with self.subTest(role=role, table=table):
                    self.context.member.roles = [fixtures.Role(roles.AGENT_TESTER_ROLE_ID), fixtures.Role(role)]
                    self.context = replace(self.context, state=type(self.context.state)())
                    result = await query_context(self.context, f"SELECT count(*) AS n FROM links.{table}")
                    self.assertEqual("error" not in result, role in roles.RECRUITERS | roles.CORE)

    async def test_verified_links_stay_public_and_review_evidence_rechecks_roles(self):
        self.context.member.roles = [fixtures.Role(roles.AGENT_TESTER_ROLE_ID)]
        result = await query_context(self.context, "SELECT count(*) AS n FROM links.links")
        self.assertNotIn("error", result)
        self.context.member.roles.append(fixtures.Role(next(iter(roles.RECRUITERS))))
        self.context.state.scope = None
        result = await query_context(self.context, "SELECT count(*) AS n FROM links.suggestions")
        self.assertNotIn("error", result)
        self.assertIn("recruiter_or_core", self.context.state.required_access)
        self.context.member.roles.pop()
        with self.assertRaises(AgentAccessLost):
            await require_evidence_access(self.context)

    async def test_shared_member_destination_excludes_review_data(self):
        self.context = replace(self.context, private_pass=False)
        self.context.source_message.channel = self.context.guild.channels[1]
        result = await query_context(self.context, "SELECT * FROM links.suggestions")
        self.assertIn("error", result)
        self.assertTrue(result["available_privately"])
