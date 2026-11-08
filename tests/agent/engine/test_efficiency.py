"""Efficiency fixes preserve requested evidence, identities and useful answers."""

from copy import deepcopy
from agent.engine.helpers import LegacyCatalogueMixin

import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.reads.cwl import ass_ranking
from elbow_helper.agent.engine.evidence_delta import EvidenceDelta, access_refused
from elbow_helper.agent.engine.steps import disclosure_issue
from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.models import RegisteredAgentTool
from elbow_helper.infrastructure.ai import AgentToolDefinition
from agent.engine.test_agent_plan_flow import _context, _model_step, _plan
from agent.engine import test_request_cost as cost

response = cost.response


def planned(*steps):
    return _model_step(_plan(list(steps)))


def step(identity, capability, arguments=None):
    return {"id": identity, "capability": capability, "arguments": arguments or {}, "depends_on": []}


def scores(count=17):
    result = {"clan_code": "BE1", "seasons": ["2026-10"], "completed_wars": 7,
            "players": [{"rank": count - index, "rank_total": count, "player_name": f"Account {index}",
                         "player_tag": f"#P{index}", "ass_score": float(index)} for index in range(count)],
            "flags": {"status": "complete", "truncated": False}}
    result["ranking"] = ass_ranking(result["players"])
    return result


class EfficiencyTests(LegacyCatalogueMixin, unittest.IsolatedAsyncioTestCase):
    flow = cost.RequestCostTests.flow


    async def test_unresolved_channel_does_not_become_a_terminal_access_refusal(self):
        context = _context()
        registry = build_agent_tools()
        planned_read = step("history", "read_discord_channel_history", {"channel_id": 61, "after": "2026-10-01"})
        with (patch("elbow_helper.agent.engine.steps.require_evidence_access", AsyncMock()),
              patch("elbow_helper.agent.engine.steps.accessible_message_channel", AsyncMock(return_value=None))):
            unavailable = await disclosure_issue(context, registry, planned_read)
            self.assertFalse(access_refused(unavailable))
            context.state.scope_audit.excluded["61"] = "role_scope"
            denied = await disclosure_issue(context, registry, planned_read)
            self.assertTrue(access_refused(denied))

    async def test_permission_failure_keeps_independent_permitted_research_available(self):
        for payload in ({"error": "The query timed out."}, {"rows": [], "row_count": 0},
                        {"error": "This needs recruiter access.", "required_access": ["recruiter"]}):
            with self.subTest(payload=payload):
                registry = {"read_value": RegisteredAgentTool(AgentToolDefinition(
                    "read_value", "Read synthetic data.", {"type": "object", "properties": {"page": {"type": "integer"}}}),
                    AsyncMock(side_effect=[payload, {"fact": "Permitted"}]))}
                # A separate successful part means an access denial cannot end the whole task.
                flow, session, _, _ = self.flow([
                    planned(step("one", "read_value", {"page": 1}), step("two", "read_value", {"page": 2})),
                    response("Permitted finding."),
                ], registry=registry)
                self.assertEqual(await flow.run(), "Permitted finding.")
                self.assertTrue(session.calls[-1][1])


    def test_knowledge_dedup_preserves_new_section_pages_and_metadata(self):
        delta = EvidenceDelta()
        section = {"section_id": "synthetic", "title": "Entry", "body": "Full policy." * 500,
                   "section_offset": 0, "next_section_offset": None}
        original = {"sections": [section], "scope": "channel", "available_privately": False}
        retained = deepcopy(original)
        delta.prepare({"first": original}, {"first": "query one"})
        emitted, changed = delta.prepare({"again": original}, {"again": "query two"})
        self.assertFalse(changed)
        self.assertEqual(emitted["again"]["sections"], [])
        self.assertEqual(emitted["again"]["reused_sections"], [{"step": "first", "path": ["sections", 0]}])
        self.assertEqual(original, retained)
        continued = {**original, "sections": [{**section, "section_offset": 4000, "body": "Continuation"}]}
        emitted, changed = delta.prepare({"page": continued})
        self.assertTrue(changed)
        self.assertEqual(emitted["page"]["sections"][0]["body"], "Continuation")
        _, changed = delta.prepare({"private": {**original, "available_privately": True}})
        self.assertTrue(changed)
        _, changed = delta.prepare({"remaining": {**original, "next_offset": 3}})
        self.assertTrue(changed)

    def test_equal_data_from_distinct_sources_does_not_stop_required_cohort_reads(self):
        delta = EvidenceDelta()
        payload = {"stars": 3}
        delta.prepare({"one": payload}, {"one": "account one"})
        emitted, changed = delta.prepare({"two": payload}, {"two": "account two"})
        self.assertTrue(changed)
        self.assertEqual(emitted["two"], payload)
