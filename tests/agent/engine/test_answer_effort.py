"""Composition and repairs use bounded effort independently of research plans."""

import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.models import RegisteredAgentTool
from elbow_helper.infrastructure.ai import AgentStep, AgentToolDefinition, AgentUsage
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from tests.agent.engine.test_agent_plan_flow import _context, _Model, _Session, _model_step, _plan, _step


class AnswerEffortTests(unittest.IsolatedAsyncioTestCase):
    async def answer(self, steps, question="Review #P0", payload=None):
        session = _Session(steps, [])
        registry = {"read_value": RegisteredAgentTool(AgentToolDefinition(
            "read_value", "Read synthetic evidence.", {"type": "object", "properties": {}}),
            AsyncMock(return_value=payload or {}))}
        with patch("elbow_helper.agent.engine.service.build_agent_tools", return_value=registry):
            answer = await AgentService(_Model(session)).answer(
                question=question, local_context="", context=_context())
        return answer, [call[2] for call in session.calls]

    async def test_writer_has_its_own_effort_even_after_high_planning(self):
        answer, efforts = await self.answer([
            _model_step(_plan([_step("read")], effort="low")),
            AgentStep("Keep recruiting.", (), AgentUsage()),
        ])
        self.assertEqual(answer, "Keep recruiting.")
        self.assertEqual(efforts, [AgentReasoningEffort.HIGH, AgentReasoningEffort.LOW])

    async def test_citation_repair_does_not_inherit_maximum_planner_effort(self):
        answer, efforts = await self.answer([
            _model_step(_plan([_step("read")], effort="max")),
            AgentStep("Keep recruiting. [[message:999]]", (), AgentUsage()),
            AgentStep("Keep recruiting.", (), AgentUsage()),
        ])
        self.assertEqual(answer, "Keep recruiting.")
        self.assertEqual(efforts, [AgentReasoningEffort.HIGH, AgentReasoningEffort.LOW, AgentReasoningEffort.LOW])


    async def test_later_plan_cannot_raise_writer_effort(self):
        answer, efforts = await self.answer([
            _model_step(_plan([_step("first")], effort="max")),
            _model_step(_plan([_step("second")], effort="low")),
            AgentStep("Keep recruiting.", (), AgentUsage()),
        ])
        self.assertEqual(answer, "Keep recruiting.")
        self.assertEqual(efforts, [AgentReasoningEffort.HIGH, AgentReasoningEffort.LOW, AgentReasoningEffort.LOW])
