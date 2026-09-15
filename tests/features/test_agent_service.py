from __future__ import annotations

from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
import unittest
import json
from unittest.mock import AsyncMock
from unittest.mock import patch

from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.configuration.roles import CORE
from elbow_helper.features.agent.prompts import SYSTEM_PROMPT
from elbow_helper.features.agent.service import CoreAgentService
from elbow_helper.features.agent.service import _valid_arguments, _bound_tool_result
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.infrastructure.ai import AgentStep
from elbow_helper.infrastructure.ai import AgentToolCall
from elbow_helper.infrastructure.ai import AgentToolDefinition
from elbow_helper.infrastructure.ai import AgentUsage


class _AgentSession:
    def __init__(self, steps: list[AgentStep]):
        self.steps = steps
        self.calls: list[tuple[tuple[object, ...], bool]] = []

    async def advance(self, tool_results=(), *, allow_tools=True):
        self.calls.append((tuple(tool_results), allow_tools))
        return self.steps.pop(0)


class _AgentModel:
    configured = True

    def __init__(self, session: _AgentSession):
        self.session = session
        self.request = None

    def create_agent_session(self, **kwargs):
        self.request = kwargs
        return self.session


def _context():
    member = SimpleNamespace(id=42, display_name="Ahmad", roles=[SimpleNamespace(id=next(iter(CORE)))])
    channel = SimpleNamespace(id=100, permissions_for=lambda member: SimpleNamespace(view_channel=True, read_message_history=True))
    message = SimpleNamespace(
        channel=channel,
        created_at=datetime(2026, 9, 14, tzinfo=timezone.utc),
    )
    guild = SimpleNamespace(id=1, name="Brown Elbow", me=member, get_member=lambda member_id: member)
    return AgentRequestContext(
        bot=None, account_links=None, clan_health=None, message_search=None,
        member=member,
        source_message=message,
        guild=guild,
    )


class CoreAgentServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_budget_forces_next_round_to_answer(self):
        handler = AsyncMock(return_value={"answer": 1})
        tool = RegisteredAgentTool(AgentToolDefinition("lookup", "test", {"properties": {}}), handler)
        session = _AgentSession([
            AgentStep("", (AgentToolCall("1", "lookup", "{}"),), AgentUsage()),
            AgentStep("done", (), AgentUsage()),
        ])
        with (
            patch("elbow_helper.features.agent.service.build_agent_tools", return_value={"lookup": tool}),
            patch("elbow_helper.features.agent.service.MAX_TOOL_CALLS", 1),
        ):
            await CoreAgentService(_AgentModel(session)).answer(question="test", local_context="", context=_context())
        self.assertFalse(session.calls[1][1])

    async def test_access_revoked_during_lookup_stops_the_request(self):
        context = _context()
        async def revoke(*args):
            context.member.roles = []
            return {"private": "evidence"}
        tool = RegisteredAgentTool(AgentToolDefinition("lookup", "test", {"properties": {}}), revoke)
        session = _AgentSession([AgentStep("", (AgentToolCall("1", "lookup", "{}"),), AgentUsage())])
        with patch("elbow_helper.features.agent.service.build_agent_tools", return_value={"lookup": tool}):
            with self.assertRaises(AgentAccessLost):
                await CoreAgentService(_AgentModel(session)).answer(question="test", local_context="", context=context)
        self.assertEqual(len(session.calls), 1)

    async def test_identical_lookup_runs_once(self):
        handler = AsyncMock(return_value={"answer": 1})
        tool = RegisteredAgentTool(AgentToolDefinition("lookup", "test", {"properties": {}}), handler)
        session = _AgentSession([
            AgentStep("", (AgentToolCall("1", "lookup", "{}"), AgentToolCall("2", "lookup", "{}")), AgentUsage()),
            AgentStep("done", (), AgentUsage()),
        ])
        with patch("elbow_helper.features.agent.service.build_agent_tools", return_value={"lookup": tool}):
            await CoreAgentService(_AgentModel(session)).answer(question="test", local_context="", context=_context())
        handler.assert_awaited_once()
        self.assertIn("already ran", session.calls[1][0][1].content)

    def test_schema_rejects_missing_unknown_and_wrongly_typed_arguments(self):
        schema = {"properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["limit"]}
        for arguments in ({}, {"limit": True}, {"limit": "3"}, {"limit": 11}, {"limit": 3, "sql": "anything"}):
            with self.subTest(arguments=arguments):
                self.assertFalse(_valid_arguments(arguments, schema))
        self.assertTrue(_valid_arguments({"limit": 3}, schema))

    def test_escaped_evidence_stays_within_encoded_budget(self):
        content = json.dumps({"data": ('"\\\n' * 1000)})
        bounded = _bound_tool_result(content, 100)
        self.assertLessEqual(len(bounded), 100)
        self.assertTrue(json.loads(bounded)["truncated"])

    async def test_casual_answer_does_not_force_a_tool_call(self) -> None:
        session = _AgentSession(
            [
                AgentStep(
                    content="He said what he said. Leave the man alone.",
                    tool_calls=(),
                    usage=AgentUsage(prompt_tokens=100, completion_tokens=20),
                )
            ]
        )
        model = _AgentModel(session)

        answer = await CoreAgentService(model).answer(
            question="tell this guy to piss off",
            local_context="Message directly replied to: he asked for another reminder",
            context=_context(),
        )

        self.assertEqual(answer, "He said what he said. Leave the man alone.")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][0], ())
        self.assertIn("tell this guy to piss off", model.request["prompt"])
        self.assertIn("he asked for another reminder", model.request["prompt"])

    async def test_requested_tool_result_is_returned_to_the_same_session(self) -> None:
        session = _AgentSession(
            [
                AgentStep(
                    content="",
                    tool_calls=(
                        AgentToolCall(
                            call_id="call-1",
                            name="lookup_test_data",
                            arguments='{"value": 7}',
                        ),
                    ),
                    usage=AgentUsage(),
                ),
                AgentStep(
                    content="The stored value is 7.",
                    tool_calls=(),
                    usage=AgentUsage(),
                ),
            ]
        )
        model = _AgentModel(session)
        handler = AsyncMock(return_value={"value": 7})
        tool = RegisteredAgentTool(
            definition=AgentToolDefinition(
                name="lookup_test_data",
                description="Look up test data",
                parameters={"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]},
            ),
            handler=handler,
        )

        with patch(
            "elbow_helper.features.agent.service.build_agent_tools",
            return_value={"lookup_test_data": tool},
        ):
            answer = await CoreAgentService(model).answer(
                question="look it up",
                local_context="",
                context=_context(),
            )

        self.assertEqual(answer, "The stored value is 7.")
        handler.assert_awaited_once()
        second_results, allow_tools = session.calls[1]
        self.assertTrue(allow_tools)
        self.assertEqual(second_results[0].call_id, "call-1")
        self.assertEqual(second_results[0].content, '{"value": 7}')

    def test_prompt_keeps_casual_context_narrow(self) -> None:
        self.assertIn("Do not search their history", SYSTEM_PROMPT)
        self.assertIn("Do not search broadly", SYSTEM_PROMPT)
        self.assertIn("Do not call several tools when one result answers", SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
