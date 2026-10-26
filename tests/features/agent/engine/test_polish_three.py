"""Regressions from retest-noon: rejected replies, parked rosters and repeated exports."""

from dataclasses import replace
from features.agent.engine.helpers import LegacyCatalogueMixin

import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.features.agent.datasets.clash import read_clash
from elbow_helper.features.agent.datasets.clash_cache import RequestClashReads
from elbow_helper.features.agent.engine.budgets import RESEARCH_LIMITS
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.engine.tool_call import bound_tool_result
from elbow_helper.features.agent.models import AgentCapabilityEffect, RegisteredAgentTool
from elbow_helper.features.agent.plan.checker import check_plan
from elbow_helper.features.agent.plan.planning import request_plan
from elbow_helper.features.agent.prompts import PERSONA_RULES
from elbow_helper.features.agent.wording import ACTION_UNAVAILABLE
from elbow_helper.infrastructure.ai import AgentToolCall, AgentToolDefinition, AgentUsage

from features.agent.engine import test_request_cost as cost
from features.agent.engine.test_agent_plan_flow import _context, _model_step, _plan, _Session, _step


def planned(identity, name="read_clash", arguments=None, **fields):
    return cost.response(calls=_model_step({**_plan([
        {**_step(identity, arguments), "capability": name},
    ]), **fields}).tool_calls)


class PlanningRegressionTests(LegacyCatalogueMixin, unittest.IsolatedAsyncioTestCase):
    def flow(self, steps, **kwargs):
        return cost.RequestCostTests.flow(self, steps, **kwargs)

    async def test_reply_request_runs_knowledge_read_before_answering(self):
        handler = AsyncMock(return_value={"sections": [{
            "title": "Clan links", "body": "Clan tags are listed in clan-info.",
        }]})
        tool = RegisteredAgentTool(AgentToolDefinition(
            "search_approved_knowledge", "Read approved knowledge.",
            {"type": "object", "properties": {}},
        ), handler)
        flow, session, _, _ = self.flow([
            planned("knowledge", "search_approved_knowledge", reply="placeholder"),
            cost.response("Clan tags are listed in clan-info."),
        ], question="Can you answer their question and point them to the right channel?",
            registry={"search_approved_knowledge": tool})
        self.assertEqual(await flow.run(), "Clan tags are listed in clan-info.")
        handler.assert_awaited_once()
        self.assertEqual(len(session.calls), 2)
        self.assertIn("Clan tags", session.calls[-1][0][0].content)
        self.assertNotIn("reply", flow.plan)

    async def test_revision_acknowledgement_is_discarded_before_artifact_execution(self):
        export = AsyncMock(return_value={"attachment_prepared": True, "rows": 1})
        registry = {
            "read_clash": RegisteredAgentTool(AgentToolDefinition(
                "read_clash", "Read accounts.", {"type": "object", "properties": {}},
            ), AsyncMock(return_value={"rows": [{"player_name": "Player A", "missed": 2}]})),
            "prepare_spreadsheet": RegisteredAgentTool(AgentToolDefinition(
                "prepare_spreadsheet", "Prepare a workbook.", {"type": "object", "properties": {}},
            ), export, AgentCapabilityEffect.ARTIFACT),
        }
        flow, session, _, _ = self.flow([
            planned("accounts"),
            planned("sheet", "prepare_spreadsheet", reply="Here is the spreadsheet."),
            cost.response("Player A missed two attacks. Attached account details."),
        ], registry=registry)
        self.assertIn("Player A", await flow.run())
        export.assert_awaited_once()
        self.assertEqual(len(session.calls), 3)

    async def test_invalid_initial_plan_gets_one_answer_only_recovery_with_reply_context(self):
        broken = cost.response(calls=(AgentToolCall("broken", "unknown_tool", "{}"),))
        flow, session, _, _ = self.flow([broken, broken])
        flow.answer_context = "Replied question: where are the clan tags? Clan links: clan-info."
        final = _Session([cost.response("Clan tags are in clan-info.")], [])
        captured = []
        def create(**kwargs):
            captured.append(kwargs)
            return final
        flow.final_session_factory = create
        self.assertEqual(await flow.run(), "Clan tags are in clan-info.")
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(len(final.calls), 1)
        self.assertFalse(final.calls[0][1])
        self.assertEqual(captured[0]["tools"], ())
        self.assertIn("Replied question", captured[0]["prompt"])
        self.assertEqual(flow.runner.tool_calls, 0)

    async def test_malformed_revisions_preserve_findings_in_answer_only_recovery(self):
        unknown = cost.response(calls=(AgentToolCall("unknown", "unknown_tool", "{}"),))
        invalid = planned("bad", output="missing")
        corrupt = cost.response(calls=(AgentToolCall("bad", "submit_request_plan", "{"),))
        for revisions in ([unknown], [invalid, unknown], [invalid, invalid], [invalid, corrupt]):
            with self.subTest(revisions=revisions):
                flow, session, _, _ = self.flow([planned("first")])
                writer = _Session(revisions, [])
                final = _Session([cost.response("The checked account is in Synthetic league.")], [])
                captured = []
                def create(**kwargs):
                    captured.append(kwargs)
                    return writer if kwargs["tools"] else final
                flow.final_session_factory = create
                self.assertIn("Synthetic league", await flow.run())
                self.assertEqual(flow.runner.tool_calls, 1)
                self.assertEqual(len(session.calls), 1)
                self.assertEqual(len(writer.calls), len(revisions))
                self.assertIn("Synthetic league", captured[0]["prompt"])
                self.assertIn("Synthetic league", captured[-1]["prompt"])
                self.assertFalse(final.calls[0][1])

    async def test_empty_feedback_answer_recovers_without_asking_to_refocus(self):
        tool = RegisteredAgentTool(AgentToolDefinition(
            "read_clash", "Read a fact.", {"type": "object", "properties": {}},
        ), AsyncMock(return_value={"status": "needs_input", "missing": ["Which season?"]}))
        flow, session, _, _ = self.flow([planned("read")], registry={"read_clash": tool})
        empty_writer = _Session([cost.response("")], [])
        final = _Session([cost.response("Which CWL season should I compare?")], [])
        sessions = iter((empty_writer, final))
        flow.final_session_factory = lambda **_: next(sessions)
        self.assertEqual(await flow.run(), "Which CWL season should I compare?")
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(empty_writer.calls[0][1])
        self.assertEqual(flow.runner.tool_calls, 1)
        self.assertEqual(len(final.calls), 1)

    async def test_failed_export_is_not_retried_and_account_evidence_reaches_final_session(self):
        export = AsyncMock(return_value={"error": ACTION_UNAVAILABLE})
        registry = {
            "read_clash": RegisteredAgentTool(AgentToolDefinition(
                "read_clash", "Read accounts.", {"type": "object", "properties": {}},
            ), AsyncMock(return_value={"rows": [{"player_name": "Player A", "missed": 2}]})),
            "prepare_spreadsheet": RegisteredAgentTool(AgentToolDefinition(
                "prepare_spreadsheet", "Prepare a workbook.", {"type": "object", "properties": {}},
            ), export, AgentCapabilityEffect.ARTIFACT),
        }
        flow, session, _, _ = self.flow([planned("accounts")], registry=registry)
        writers = [_Session([planned("sheet", "prepare_spreadsheet")], []),
                   _Session([planned("retry_sheet", "prepare_spreadsheet")], [])]
        final = _Session([cost.response("Player A missed two attacks; keep a reserve account.")], [])
        captured = []
        sessions = iter((*writers, final))
        def create(**kwargs):
            captured.append(kwargs)
            return next(sessions)
        flow.final_session_factory = create
        self.assertIn("keep a reserve", await flow.run())
        export.assert_awaited_once()
        self.assertEqual(flow.runner.tool_calls, 2)
        self.assertIn("Do not retry those outputs", captured[1]["prompt"])
        self.assertEqual(len(session.calls), 1)
        self.assertEqual([len(writer.calls) for writer in writers], [1, 1])
        self.assertIn("Player A", captured[0]["prompt"])
        self.assertIn("Attachment failures do not make retrieved rows unavailable", captured[-1]["system_prompt"])

    async def test_recorded_size_research_compacts_before_323k_tokens(self):
        async def read(_, arguments):
            return {"player_name": "Account " + str(arguments["batch"])}
        reader = AsyncMock(side_effect=read)
        registry = {"read_clash": RegisteredAgentTool(AgentToolDefinition(
            "read_clash", "Read another account batch.", {"type": "object", "properties": {
                "batch": {"type": "integer"},
            }},
        ), reader)}
        plans = [replace(planned(str(i), arguments={"batch": i}), usage=AgentUsage(size, 700))
                 for i, size in enumerate((33_569, 45_980, 59_472, 66_104))]
        flow, session, _, _ = self.flow(plans[:1], registry=registry)
        writers = [_Session([step], []) for step in plans[1:3]]
        final = _Session([replace(cost.response("Reliability assessment."),
                                  usage=AgentUsage(6000, 800))], [])
        sessions = iter((*writers, final))
        flow.final_session_factory = lambda **_: next(sessions)
        self.assertEqual(await flow.run(), "Reliability assessment.")
        self.assertLessEqual(flow.budget.spent_input, RESEARCH_LIMITS.input_tokens)
        self.assertLess(flow.budget.spent_input, 200_000)
        self.assertLessEqual(flow.budget.spent_rounds, RESEARCH_LIMITS.rounds)
        self.assertTrue(flow.rounder.final_attempted)
        self.assertEqual(len(final.calls), 1)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual([len(writer.calls) for writer in writers], [1, 1])
        self.assertEqual(reader.await_count, 3)

    def test_normalization_preserves_checks_and_valid_social_replies(self):
        tool = RegisteredAgentTool(AgentToolDefinition(
            "read_value", "Read a value.", {"type": "object", "properties": {}},
        ), AsyncMock())
        for reply in ("On it.", "placeholder", ""):
            with self.subTest(reply=reply):
                plan = request_plan(_model_step({**_plan([_step("read")]), "reply": reply}))
                self.assertNotIn("reply", plan)
                self.assertTrue(check_plan(plan, {"read_value": tool}).ok)
        malformed = request_plan(_model_step({**_plan([_step("read")]), "reply": 7}))
        self.assertFalse(check_plan(malformed, {"read_value": tool}).ok)
        social = {**_plan([]), "reply": "Hello."}
        self.assertEqual(request_plan(_model_step(social)), social)
        invalid = request_plan(_model_step({**_plan([_step("read")]), "reply": "On it.", "output": "missing"}))
        self.assertFalse(check_plan(invalid, {"read_value": tool}).ok)

    def test_roast_policy_uses_known_address_without_forbidding_game_unit_names(self):
        self.assertIn("filler address terms", PERSONA_RULES)
        self.assertIn("Refer to members by their server nickname", PERSONA_RULES)
        self.assertNotIn("Do not invent pet names", PERSONA_RULES)


class CwlMembershipRegressionTests(LegacyCatalogueMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        access = patch("elbow_helper.features.agent.datasets.clash.require_evidence_access", AsyncMock())
        access.start()
        self.addCleanup(access.stop)
        self.payload = {"tag": CLANS["BE4"].tag, "name": "Brown Elbow IV", "members": 8,
                        "memberList": [{"tag": "#P0", "name": "Parked account", "townHallLevel": 3}]}
        self.client = SimpleNamespace(configured=True, get=AsyncMock(return_value=SimpleNamespace(
            ok=True, status=200, payload=self.payload)))
        self.context = replace(_context(), bot=SimpleNamespace(clash_client=self.client))

    async def test_placeholder_roster_is_withheld_for_every_projection_and_tag_spelling(self):
        for tag in ("BE4", CLANS["BE4"].tag):
            for projection in ({}, {"detail": "full"}, {"fields": ["members", "memberList"]},
                               {"fields": ["memberList.townHallLevel"]}):
                with self.subTest(tag=tag, projection=projection):
                    result = await read_clash(self.context, {"kind": "clan", "tags": [tag], **projection})
                    row = result["items"][0]
                    self.assertNotIn("members", row["data"])
                    self.assertNotIn("memberList", row["data"])
                    self.assertEqual(row["member_cohort"]["basis"], "seasonal_cwl")
                    self.assertNotIn("Parked account", json.dumps(result))
        self.assertEqual(self.payload["members"], 8)
        self.assertEqual(len(self.payload["memberList"]), 1)

    async def test_explicit_current_roster_remains_available_from_same_cache(self):
        self.context = replace(self.context, clash_reads=RequestClashReads())
        await read_clash(self.context, {"kind": "clan", "tags": ["BE4"]})
        current = await read_clash(self.context, {"kind": "clan", "tags": ["BE4"], "member_scope": "current"})
        self.assertEqual(current["items"][0]["data"]["members"], 8)
        self.assertEqual(current["items"][0]["data"]["memberList"][0]["name"], "Parked account")
        self.assertNotIn("member_cohort", current["items"][0])
        self.client.get.assert_awaited_once()

    async def test_size_compaction_does_not_recreate_an_empty_placeholder_roster(self):
        self.payload["description"] = "Bulk field " * 2000
        result = await read_clash(self.context, {"kind": "clan", "tags": ["BE4"], "detail": "full"})
        bounded = json.loads(bound_tool_result(json.dumps(result), 2500))
        self.assertNotIn("memberList", bounded["items"][0]["data"])
        self.assertEqual(bounded["items"][0]["member_cohort"]["basis"], "seasonal_cwl")

    async def test_other_clans_follow_configured_membership_basis(self):
        result = await read_clash(self.context, {"kind": "clan", "tags": ["BEC"]})
        self.assertEqual(result["items"][0]["data"]["members"], 8)
        other = replace(CLANS["BEC"], cwl_only=True)
        with patch.dict(CLANS, {"BEC": other}):
            result = await read_clash(self.context, {"kind": "clan", "tags": ["BEC"]})
        self.assertNotIn("members", result["items"][0]["data"])
        self.assertEqual(result["items"][0]["member_cohort"]["clan_code"], "BEC")

    async def test_cwl_membership_question_bypasses_current_count_shortcut(self):
        from features.agent.engine.test_agent_plan_flow import _Model
        model = _Model(_Session([cost.response("The seasonal CWL roster is needed for that count.")], []))
        with patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value={}):
            answer = await AgentService(model).answer(
                question="How many members are in BE4?", local_context="", context=self.context)
        self.assertIn("seasonal CWL roster", answer)
        self.client.get.assert_not_awaited()
