"""Rejected replies, parked rosters and repeated exports retain safe recovery."""

from dataclasses import replace
from tests.agent.engine.helpers import LegacyCatalogueMixin

import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.reads.clash import read_clash
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.models import AgentCapabilityEffect, RegisteredAgentTool
from elbow_helper.agent.conversation.wording import ACTION_UNAVAILABLE
from elbow_helper.infrastructure.ai import AgentToolCall, AgentToolDefinition

from tests.agent.engine import test_request_cost as cost
from tests.agent.engine.test_agent_plan_flow import _context, _model_step, _plan, _Session, _step


def planned(identity, name="read_clash", arguments=None, **fields):
    return cost.response(calls=_model_step({**_plan([
        {**_step(identity, arguments), "capability": name},
    ]), **fields}).tool_calls)


class PlanningRegressionTests(LegacyCatalogueMixin, unittest.IsolatedAsyncioTestCase):
    def flow(self, steps, **kwargs):
        return cost.RequestCostTests.flow(self, steps, **kwargs)


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
        self.assertIn("A failed attachment does not make retrieved rows unavailable", captured[-1]["system_prompt"])


class CwlMembershipRegressionTests(LegacyCatalogueMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        access = patch("elbow_helper.agent.reads.clash.require_evidence_access", AsyncMock())
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


    async def test_cwl_membership_question_bypasses_current_count_shortcut(self):
        from tests.agent.engine.test_agent_plan_flow import _Model
        model = _Model(_Session([cost.response("The seasonal CWL roster is needed for that count.")], []))
        with patch("elbow_helper.agent.engine.service.build_agent_tools", return_value={}):
            answer = await AgentService(model).answer(
                question="How many members are in BE4?", local_context="", context=self.context)
        self.assertIn("seasonal CWL roster", answer)
        self.client.get.assert_not_awaited()
