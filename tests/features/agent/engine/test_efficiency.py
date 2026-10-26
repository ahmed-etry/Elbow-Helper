"""Efficiency fixes preserve requested evidence, identities and useful answers."""

from copy import deepcopy
from dataclasses import replace
from features.agent.engine.helpers import LegacyCatalogueMixin

import json
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.capabilities.cwl.scoring import ass_ranking
from elbow_helper.features.agent.engine.evidence_delta import EvidenceDelta, access_refused
from elbow_helper.features.agent.engine.steps import disclosure_issue
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.features.agent.plan.format import system_instructions
from elbow_helper.infrastructure.ai import AgentToolDefinition
from features.agent.engine.test_agent_plan_flow import _context, _model_step, _plan
from features.agent.engine import test_request_cost as cost

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

    def test_actual_prefix_supplies_chain_paths_and_timezone_requirement(self):
        prefix = system_instructions(build_agent_tools())
        self.assertIn("returns members:[{member_id,display_name,joined_at}]", prefix)
        self.assertIn("returns accounts:[{member_id,player_tag,player_name}]", prefix)
        self.assertIn("returns rows:[{SQL column names}]", prefix)
        self.assertIn("ISO8601 with timezone", prefix)
        self.assertIn("Do not spend a model round merely copying IDs", prefix)

    async def test_cohort_links_profiles_and_requirements_share_one_plan(self):
        registry = build_agent_tools()
        handlers = {
            "read_discord_members": AsyncMock(return_value={"members": [{"member_id": 1}]}),
            "read_linked_accounts": AsyncMock(return_value={"accounts": [{"member_id": 1, "player_tag": "#P0Y"}]}),
            "read_clash": AsyncMock(return_value={"source": "clash_api", "kind": "player", "items": []}),
            "search_approved_knowledge": AsyncMock(return_value={"sections": [{"title": "Entry", "body": "War evidence required."}]}),
        }
        registry = {name: replace(registry[name], handler=handler) for name, handler in handlers.items()}
        flow, session, _, _ = self.flow([
            planned(step("cohort", "read_discord_members", {"joined_after": "2026-10-01T00:00:00Z"}),
                    step("links", "read_linked_accounts", {"member_ids": {"step": "cohort", "path": ["members", "*", "member_id"]}}),
                    step("profiles", "read_clash", {"kind": "player", "tags": {"step": "links", "path": ["accounts", "*", "player_tag"]}}),
                    step("rules", "search_approved_knowledge", {"titles": ["Entry"]})),
            response("The linked profile is resolved; war evidence is still needed."),
        ], registry=registry)
        answer = await flow.run()
        self.assertIn("war evidence", answer)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(flow.runner.tool_calls, 4)
        self.assertEqual(handlers["read_linked_accounts"].call_args.args[1]["member_ids"], [1])
        self.assertEqual(handlers["read_clash"].call_args.args[1]["tags"], ["#P0Y"])

    async def test_cached_report_is_referenced_but_full_evidence_remains(self):
        handler = AsyncMock(return_value={"league": "Synthetic league"})
        registry = {"read_value": RegisteredAgentTool(AgentToolDefinition(
            "read_value", "Read a synthetic fact.", {"type": "object", "properties": {}}), handler)}
        flow, session, _, _ = self.flow([
            planned(step("first", "read_value")), planned(step("again", "read_value")), response("Synthetic league."),
        ], registry=registry)
        self.assertEqual(await flow.run(), "Synthetic league.")
        handler.assert_awaited_once()
        emitted = json.loads(session.calls[-1][0][0].content)["results"]["again"]
        self.assertEqual(emitted["reused_result"], {"step": "first", "path": []})
        self.assertEqual(flow.context.state.grounding_evidence["again"]["league"], "Synthetic league")
        self.assertFalse(session.calls[-1][1])

    async def test_denied_only_batch_answers_without_another_research_round(self):
        registry = {"read_notes": RegisteredAgentTool(AgentToolDefinition(
            "read_notes", "Read synthetic notes.", {"type": "object", "properties": {}}),
            AsyncMock(return_value={"error": "This needs recruiter access.", "required_access": ["recruiter"]}))}
        flow, session, _, _ = self.flow([
            planned(step("notes", "read_notes")), response("Recruiter notes are for Recruiters."),
        ], registry=registry)
        self.assertEqual(await flow.run(), "Recruiter notes are for Recruiters.")
        self.assertFalse(session.calls[-1][1])
        self.assertIn("Stop that restricted part", session.calls[-1][0][0].content)

    async def test_unresolved_channel_does_not_become_a_terminal_access_refusal(self):
        context = _context()
        registry = build_agent_tools()
        planned_read = step("history", "read_discord_channel_history", {"channel_id": 61, "after": "2026-10-01"})
        with (patch("elbow_helper.features.agent.engine.steps.require_evidence_access", AsyncMock()),
              patch("elbow_helper.features.agent.engine.steps.accessible_message_channel", AsyncMock(return_value=None))):
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

    async def test_ass_query_and_score_supply_complete_ranking_to_the_answer_model(self):
        payload = scores()
        final = "BE1 ASS\n```text\n" + payload["ranking"]["text"] + "\n```"
        registry = build_agent_tools()
        registry = {name: replace(registry[name], handler=handler) for name, handler in {
            "query_bot_data": AsyncMock(return_value={"rows": [{"war_id": "CWL:synthetic"}], "row_count": 1}),
            "cwl_ass_scores": AsyncMock(return_value=payload),
        }.items()}
        flow, session, _, _ = self.flow([planned(
            step("wars", "query_bot_data", {"sql": "SELECT war_id FROM health.wars WHERE clan_code='BE1'"}),
            step("ass", "cwl_ass_scores", {"clan_code": "BE1", "war_ids": {"step": "wars", "path": ["rows", "*", "war_id"]}}),
        ), response(final)], question="What is the ASS score for BE1 from this past month?", registry=registry)
        answer = await flow.run()
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(answer, final)
        emitted = json.loads(session.calls[-1][0][0].content)["results"]["ass"]
        self.assertEqual(emitted["ranking"], payload["ranking"])
        for index in range(17):
            self.assertIn(f"Account {index}\t#P{index}\t", answer)
        self.assertLess(answer.index("Account 16"), answer.index("Account 0"))
        self.assertNotIn("average", answer)
        self.assertNotIn("per account", answer)

    async def test_ass_lookup_preserves_the_models_answer_and_format(self):
        registry = build_agent_tools()
        registry = {"cwl_ass_scores": replace(registry["cwl_ass_scores"], handler=AsyncMock(return_value=scores()))}
        for question in ("What is the ASS score for BE1 from this past month?", "Classement ASS BE1, explique les scores."):
            with self.subTest(question=question):
                final = "Model-selected answer and format."
                flow, session, _, _ = self.flow([planned(step("ass", "cwl_ass_scores", {
                    "clan_code": "BE1", "war_ids": ["CWL:synthetic"],
                })), response(final)], question=question, registry=registry)
                self.assertEqual(await flow.run(), final)
                self.assertEqual(len(session.calls), 2)
                self.assertEqual(flow.context.state.attachments, [])

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
