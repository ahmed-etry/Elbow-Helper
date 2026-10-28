"""Failed reads must not poison corrected evidence or terminate recovery."""

from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.models import RegisteredAgentTool
from elbow_helper.infrastructure.ai import AgentStep, AgentToolDefinition, AgentUsage
from agent.engine.test_agent_plan_flow import _Model, _Session, _context, _model_step, _plan
from agent.engine.test_strict_answers import players, query


class AnswerRecoveryFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_strict_requests_recover_after_failure_in_the_original_request(self):
        for question, result, expected in (("names only", players(), "North\nSouth"),
                                           ("tags only", players(), "#P0\n#P2"),
                                           ("count only", query([{"count": 2}]), "2")):
            for reuse in (False, True):
                with self.subTest(question=question, reuse=reuse):
                    handler = AsyncMock(side_effect=[{"error": "The first read failed."}, result])
                    registry = {"read_clash": RegisteredAgentTool(AgentToolDefinition(
                        "read_clash", "Read selected records.", {"type": "object", "properties": {
                            "selection": {"type": "string"}}, "required": ["selection"]}), handler)}
                    def step(identity, selection):
                        return replace(_model_step(_plan([{"id": identity, "capability": "read_clash",
                            "arguments": {"selection": selection}, "reason": "Read selected records", "depends_on": []}])),
                            usage=AgentUsage(1000, 100))
                    session = _Session([step("read", "old"), step("read" if reuse else "corrected", "new"),
                                        AgentStep(expected, (), AgentUsage(1000, 100))], [])
                    with patch("elbow_helper.agent.engine.service.build_agent_tools", return_value=registry):
                        answer = await AgentService(_Model(session)).answer(
                            question=question, local_context="", context=_context())
                    self.assertEqual(answer, expected)
                    self.assertEqual(handler.await_count, 2)
                    self.assertEqual(len(session.calls), 3)
