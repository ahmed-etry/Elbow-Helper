"""Only checked, executed plan goals reach the writer, in order within a request."""

from dataclasses import replace
import json
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.rounds import AgentLimitReached
from elbow_helper.agent.models import RegisteredAgentTool
from elbow_helper.agent.plan.format import answer_instructions
from elbow_helper.infrastructure.ai import AgentStep, AgentToolDefinition, AgentUsage
from agent.engine.test_agent_plan_flow import _context, _Model, _Session, _model_step, _plan, _step


class AnswerGoalTests(unittest.IsolatedAsyncioTestCase):
    def start_flow(self, session):
        context = _context()
        context.state.request_text = "Find a member's activity in the server."
        source = AsyncMock(return_value={"value": 7})
        registry = {"read_value": RegisteredAgentTool(AgentToolDefinition(
            "read_value", "Read synthetic evidence.", {"type": "object", "properties": {
                "value": {"type": "integer"}}}), source)}
        with patch("elbow_helper.agent.engine.service.build_agent_tools", return_value=registry):
            flow, _ = AgentService(_Model(session))._start_flow(
                question=context.state.request_text, local_context="",
                context=context, conversation_history="")
        return flow, source

    async def test_initial_and_revised_corrections_preserve_only_executed_goals(self):
        goals = ["Rejected initial goal", "Find the member's server activity",
                 "Rejected revision goal", "Read their most recent messages"]
        plans = []
        for index, goal in enumerate(goals):
            step = _step("identity" if index < 2 else "messages", {"value": index // 2})
            if index % 2 == 0:
                step["capability"] = "missing_read"
            plans.append({**_plan([step]), "goal": goal})
        steps = [replace(_model_step(plan), usage=AgentUsage(1000, 100)) for plan in plans]
        session = _Session([*steps, AgentStep("Synthetic answer.", (), AgentUsage(1000, 100))], [])
        flow, source = self.start_flow(session)

        self.assertEqual(await flow.run(), "Synthetic answer.")
        self.assertEqual(source.await_count, 2)
        for allow_more in (True, False, True):
            frame = json.loads(flow._answer_frame({}, allow_more=allow_more, instruction=""))
            self.assertEqual(frame["asker_wants"], goals[1::2])
            self.assertEqual(frame["request"], "Find a member's activity in the server.")
            for goal in goals:
                self.assertNotIn(goal, flow._answer_system(allow_more=allow_more))

        another, _ = self.start_flow(_Session([], []))
        frame = json.loads(another._answer_frame({}, allow_more=False, instruction=""))
        self.assertEqual(frame["asker_wants"], [])

    async def test_reply_only_plan_has_no_executed_goal(self):
        plan = {**_plan([]), "goal": "Reply without a read", "reply": "Synthetic answer."}
        flow, source = self.start_flow(_Session([
            replace(_model_step(plan), usage=AgentUsage(1000, 100))], []))

        self.assertEqual(await flow.run(), "Synthetic answer.")
        source.assert_not_awaited()
        self.assertEqual(json.loads(flow._answer_frame({}, allow_more=False, instruction=""))["asker_wants"], [])

    async def test_checked_plan_stopped_before_execution_has_no_goal(self):
        plan = {**_plan([_step("identity")]), "goal": "Read the member's server activity"}
        flow, source = self.start_flow(_Session([
            replace(_model_step(plan), usage=AgentUsage(1000, 100)),
            AgentStep("Synthetic limit answer.", (), AgentUsage(1000, 100))], []))
        with patch.object(flow.runner, "run", AsyncMock(side_effect=AgentLimitReached("Synthetic limit"))):
            await flow.run()

        source.assert_not_awaited()
        self.assertEqual(json.loads(flow._answer_frame({}, allow_more=False, instruction=""))["asker_wants"], [])

    async def test_executed_plan_keeps_its_goal_when_the_source_is_unavailable(self):
        plan = {**_plan([_step("identity")]), "goal": "Read the member's server activity"}
        flow, source = self.start_flow(_Session([
            replace(_model_step(plan), usage=AgentUsage(1000, 100)),
            AgentStep("Synthetic source unavailable.", (), AgentUsage(1000, 100))], []))
        source.return_value = {"error": "Synthetic source unavailable.", "cause": "source_unavailable"}

        self.assertEqual(await flow.run(), "Synthetic source unavailable.")
        source.assert_awaited_once()
        self.assertEqual(json.loads(flow._answer_frame({}, allow_more=False, instruction=""))["asker_wants"],
                         [plan["goal"]])

    def test_malformed_plans_without_text_goals_leave_an_empty_answer_frame(self):
        flow, _ = self.start_flow(_Session([], []))
        for plan in (None, [], {}, {"goal": None}, {"goal": 7}):
            self.assertEqual(flow._limit_plan(plan), plan)
        for allow_more in (True, False):
            frame = json.loads(flow._answer_frame({}, allow_more=allow_more, instruction=""))
            self.assertEqual(frame["asker_wants"], [])

    def test_writer_instructions_prioritize_the_request_over_plan_goals(self):
        flow, _ = self.start_flow(_Session([], []))
        sentence = ("Answer what asker_wants describes and leave out facts from the results that don't serve it; "
                    "when it conflicts with the request, the request wins.")
        for prompt in (answer_instructions(), answer_instructions(flow.registry)):
            self.assertIn(sentence, " ".join(prompt.split()))
