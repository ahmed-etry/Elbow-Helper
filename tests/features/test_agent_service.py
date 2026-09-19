from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
import unittest
import json
from unittest.mock import AsyncMock
from unittest.mock import patch

from elbow_helper.features.agent.models import AgentAttachment, RegisteredAgentTool
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.configuration.roles import CORE, LEAD_PLUS
from elbow_helper.features.agent.prompts import SYSTEM_PROMPT
from elbow_helper.features.agent.service import CoreAgentService
from elbow_helper.features.agent.service import _valid_arguments, _bound_tool_result
from elbow_helper.features.agent.access import ACCESS_LEAD_PLUS, AgentAccessLost
from elbow_helper.features.agent.reports.roles import RoleAccountReport
from elbow_helper.infrastructure.ai import AgentStep
from elbow_helper.infrastructure.ai import AgentToolCall
from elbow_helper.infrastructure.ai import AgentToolDefinition
from elbow_helper.infrastructure.ai import AgentUsage
from elbow_helper.infrastructure.ai import TextGenerationError
from elbow_helper.features.agent.service import AgentUnavailableError


class _AgentSession:
    def __init__(self, steps: list[AgentStep]):
        self.steps = steps
        self.calls: list[tuple[tuple[object, ...], bool]] = []
        self.tool_replacements = []

    def replace_tools(self, tools):
        self.tool_replacements.append(tuple(tools))

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
    async def test_model_discovers_bounded_groups_before_cross_feature_tools(self):
        session = _AgentSession([
            AgentStep("", (AgentToolCall(
                "discover", "discover_agent_tools", '{"groups":["files"]}',
            ),), AgentUsage()),
            AgentStep("", (AgentToolCall(
                "list", "list_supported_attachments", "{}",
            ),), AgentUsage()),
            AgentStep("No supported attachments were included.", (), AgentUsage()),
        ])
        model = _AgentModel(session)

        answer = await CoreAgentService(model).answer(
            question="What can you read from the attached file?",
            local_context="", context=_context(),
        )

        initial_names = {tool.name for tool in model.request["tools"]}
        self.assertIn("discover_agent_tools", initial_names)
        self.assertIn("read_conversation_history", initial_names)
        self.assertNotIn("list_supported_attachments", initial_names)
        replacement_names = {
            tool.name for tool in session.tool_replacements[0]
        }
        self.assertIn("list_supported_attachments", replacement_names)
        self.assertNotIn("read_regular_war", replacement_names)
        self.assertIn("available_tools", session.calls[1][0][0].content)
        self.assertEqual(answer, "No supported attachments were included.")

    async def test_reselected_groups_make_earlier_optional_tools_unavailable(self):
        session = _AgentSession([
            AgentStep("", (AgentToolCall(
                "files", "discover_agent_tools", '{"groups":["files"]}',
            ),), AgentUsage()),
            AgentStep("", (AgentToolCall(
                "wars", "discover_agent_tools", '{"groups":["wars"]}',
            ),), AgentUsage()),
            AgentStep("", (AgentToolCall(
                "stale", "list_supported_attachments", "{}",
            ),), AgentUsage()),
            AgentStep("The earlier file tools are no longer selected.", (), AgentUsage()),
        ])
        answer = await CoreAgentService(_AgentModel(session)).answer(
            question="Switch from files to war evidence",
            local_context="", context=_context(),
        )

        self.assertEqual(len(session.tool_replacements), 2)
        final_names = {
            tool.name for tool in session.tool_replacements[-1]
        }
        self.assertIn("read_regular_war", final_names)
        self.assertNotIn("list_supported_attachments", final_names)
        self.assertIn("not available", session.calls[3][0][0].content)
        self.assertEqual(
            answer, "The earlier file tools are no longer selected.",
        )

    async def test_failed_tool_restores_request_local_state_exactly(self):
        context = _context()
        original = RoleAccountReport("original", "2026-09-17", (), ())
        replacement = RoleAccountReport("replacement", "2026-09-18", (), ())
        context.state.source_channels = {100, 200}
        context.state.required_access = {ACCESS_LEAD_PLUS}
        context.state.reports = {original.report_id: original}
        context.state.report_sources = {
            original.report_id: frozenset({100}),
        }
        context.state.report_access_requirements = {
            original.report_id: frozenset({ACCESS_LEAD_PLUS}),
        }
        context.state.attachments = [AgentAttachment("before.xlsx", b"before")]
        context.state.history_status = {"included_turns": 2}

        async def failing_handler(*_):
            context.state.source_channels.add(999)
            context.state.required_access.clear()
            context.state.reports.clear()
            context.state.reports[replacement.report_id] = replacement
            context.state.report_sources.clear()
            context.state.report_access_requirements.clear()
            context.state.attachments.append(AgentAttachment("after.xlsx", b"after"))
            context.state.history_status["included_turns"] = 99
            raise RuntimeError("simulated failure")

        with self.assertLogs(
            "elbow_helper.features.agent.service", level="ERROR",
        ):
            result = await CoreAgentService._execute_tool(
                name="failing", handler=failing_handler, arguments={},
                context=context,
            )
        self.assertIn("failed", result)
        self.assertEqual(context.state.source_channels, {100, 200})
        self.assertEqual(context.state.required_access, {ACCESS_LEAD_PLUS})
        self.assertEqual(context.state.reports, {"original": original})
        self.assertEqual(
            context.state.report_sources, {"original": frozenset({100})},
        )
        self.assertEqual(
            context.state.report_access_requirements,
            {"original": frozenset({ACCESS_LEAD_PLUS})},
        )
        self.assertEqual(
            context.state.attachments,
            [AgentAttachment("before.xlsx", b"before")],
        )
        self.assertEqual(context.state.history_status, {"included_turns": 2})

    async def test_access_loss_after_tool_rolls_back_new_report_and_eviction(self):
        context = _context()
        original = RoleAccountReport("original", "2026-09-17", (), ())
        replacement = RoleAccountReport("replacement", "2026-09-18", (), ())
        context.state.reports[original.report_id] = original
        context.state.report_sources[original.report_id] = frozenset({100})
        context.state.report_access_requirements[original.report_id] = frozenset()

        async def lookup(*_):
            context.state.reports.clear()
            context.state.reports[replacement.report_id] = replacement
            return {"report_id": replacement.report_id}

        with patch(
            "elbow_helper.features.agent.service.require_evidence_access",
            new=AsyncMock(side_effect=AgentAccessLost("lost")),
        ):
            with self.assertRaises(AgentAccessLost):
                await CoreAgentService._execute_tool(
                    name="lookup", handler=lookup, arguments={}, context=context,
                )
        self.assertEqual(context.state.reports, {"original": original})
        self.assertEqual(
            context.state.report_sources, {"original": frozenset({100})},
        )

    async def test_cancelled_tool_rolls_back_before_propagating(self):
        context = _context()
        original = RoleAccountReport("original", "2026-09-17", (), ())
        context.state.reports[original.report_id] = original

        async def cancelled(*_):
            context.state.reports.clear()
            context.state.attachments.append(AgentAttachment("partial.xlsx", b"x"))
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await CoreAgentService._execute_tool(
                name="cancelled", handler=cancelled, arguments={}, context=context,
            )
        self.assertEqual(context.state.reports, {"original": original})
        self.assertEqual(context.state.attachments, [])

    async def test_unexpected_base_exception_rolls_back_before_propagating(self):
        context = _context()
        original = RoleAccountReport("original", "2026-09-17", (), ())
        context.state.reports[original.report_id] = original

        async def broken(*_):
            context.state.reports.clear()
            context.state.source_channels.add(999)
            raise AssertionError("simulated invariant failure")

        with self.assertRaises(AssertionError):
            await CoreAgentService._execute_tool(
                name="broken", handler=broken, arguments={}, context=context,
            )
        self.assertEqual(context.state.reports, {"original": original})
        self.assertEqual(context.state.source_channels, set())

    async def test_new_report_records_cumulative_source_and_role_provenance(self):
        context = _context()
        context.state.source_channels.update({100, 200})
        context.state.required_access.add(ACCESS_LEAD_PLUS)
        context.member.roles.append(SimpleNamespace(id=next(iter(LEAD_PLUS))))
        report = RoleAccountReport("report", "2026-09-17", (), ())

        async def lookup(*args):
            context.state.reports[report.report_id] = report
            return {"report_id": report.report_id}

        tool = RegisteredAgentTool(
            AgentToolDefinition("lookup", "test", {"properties": {}}), lookup,
        )
        session = _AgentSession([
            AgentStep("", (AgentToolCall("1", "lookup", "{}"),), AgentUsage()),
            AgentStep("done", (), AgentUsage()),
        ])
        with (
            patch("elbow_helper.features.agent.service.build_agent_tools", return_value={"lookup": tool}),
            patch("elbow_helper.features.agent.access.accessible_message_channel", return_value=object()),
        ):
            await CoreAgentService(_AgentModel(session)).answer(
                question="test", local_context="", context=context,
            )
        self.assertEqual(context.state.report_sources["report"], frozenset({100, 200}))
        self.assertEqual(
            context.state.report_access_requirements["report"],
            frozenset({ACCESS_LEAD_PLUS}),
        )

    async def test_round_and_tool_diagnostics_include_identity_and_duration(self):
        context = _context()
        tool = RegisteredAgentTool(
            AgentToolDefinition("lookup", "test", {"properties": {}}),
            AsyncMock(return_value={"answer": 7}),
        )
        session = _AgentSession([
            AgentStep(
                "", (AgentToolCall("1", "lookup", "{}"),), AgentUsage(),
                provider_request_id="provider-1", model_identity="model-a",
                provider_duration_ms=12,
            ),
            AgentStep(
                "done", (), AgentUsage(), provider_request_id="provider-2",
                model_identity="model-a", provider_duration_ms=8,
            ),
        ])
        with (
            patch(
                "elbow_helper.features.agent.service.build_agent_tools",
                return_value={"lookup": tool},
            ),
            self.assertLogs(
                "elbow_helper.features.agent.service", level="INFO",
            ) as logs,
        ):
            result = await CoreAgentService(_AgentModel(session)).answer(
                question="test", local_context="", context=context,
            )
        self.assertEqual(result, "done")
        rendered = "\n".join(logs.output)
        self.assertIn(
            "round=1 outcome=completed", rendered,
        )
        self.assertIn("provider_request_id=provider-1", rendered)
        self.assertIn("provider_duration_ms=12", rendered)
        self.assertIn("model=model-a", rendered)
        self.assertIn("tool=lookup invoker=42 outcome=completed", rendered)

    async def test_required_role_revoked_during_lookup_stops_before_second_round(self):
        context = _context()
        context.guild.me = SimpleNamespace(
            id=999, roles=[SimpleNamespace(id=next(iter(CORE)))],
        )
        context.member.roles.append(SimpleNamespace(id=next(iter(LEAD_PLUS))))
        context.state.required_access.add(ACCESS_LEAD_PLUS)

        async def lookup(*args):
            context.member.roles = [
                role for role in context.member.roles if role.id not in LEAD_PLUS
            ]
            return {"restricted": "evidence"}

        tool = RegisteredAgentTool(
            AgentToolDefinition("lookup", "test", {"properties": {}}), lookup,
        )
        session = _AgentSession([
            AgentStep("", (AgentToolCall("1", "lookup", "{}"),), AgentUsage()),
            AgentStep("should not run", (), AgentUsage()),
        ])
        with patch(
            "elbow_helper.features.agent.service.build_agent_tools",
            return_value={"lookup": tool},
        ):
            with self.assertRaises(AgentAccessLost):
                await CoreAgentService(_AgentModel(session)).answer(
                    question="test", local_context="", context=context,
                )
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(context.state.evidence, [])

    async def test_context_pressure_requests_final_answer_without_lowering_output_limit(self):
        session = _AgentSession([AgentStep("Answer from supplied context", (), AgentUsage())])
        session.context_window_tokens = 150_000
        model = _AgentModel(session)
        await CoreAgentService(model).answer(question="test", local_context="", context=_context())
        self.assertFalse(session.calls[0][1])
        self.assertEqual(model.request["max_output_tokens"], 64_000)

    async def test_observed_later_round_pressure_stops_further_tool_requests(self):
        tool = RegisteredAgentTool(AgentToolDefinition("lookup", "test", {"properties": {}}), AsyncMock(return_value={"answer": 7}))
        session = _AgentSession([
            AgentStep("", (AgentToolCall("1", "lookup", "{}"),), AgentUsage(820_000, 64_000)),
            AgentStep("The answer is seven", (), AgentUsage()),
        ])
        session.context_window_tokens = 1_000_000
        with patch("elbow_helper.features.agent.service.build_agent_tools", return_value={"lookup": tool}):
            await CoreAgentService(_AgentModel(session)).answer(question="test", local_context="", context=_context())
        self.assertTrue(session.calls[0][1])
        self.assertFalse(session.calls[1][1])
        tool.handler.assert_awaited_once()

    async def test_exhausted_context_does_not_run_tools_or_send_another_provider_request(self):
        tool = RegisteredAgentTool(AgentToolDefinition("lookup", "test", {"properties": {}}), AsyncMock())
        session = _AgentSession([
            AgentStep("", (AgentToolCall("1", "lookup", "{}"),), AgentUsage(990_000, 64_000)),
        ])
        session.context_window_tokens = 1_000_000
        with patch("elbow_helper.features.agent.service.build_agent_tools", return_value={"lookup": tool}):
            with self.assertRaises(AgentUnavailableError):
                await CoreAgentService(_AgentModel(session)).answer(question="test", local_context="", context=_context())
        self.assertEqual(len(session.calls), 1)
        tool.handler.assert_not_awaited()

    async def test_interrupted_provider_round_is_logged_as_unknown_usage(self):
        for error, expected_exception, status in (
            (TextGenerationError("unavailable"), AgentUnavailableError, "provider_error"),
            (asyncio.CancelledError(), asyncio.CancelledError, "cancelled"),
        ):
            with self.subTest(status=status):
                session = SimpleNamespace(advance=AsyncMock(side_effect=error))
                with self.assertLogs("elbow_helper.features.agent.service", level="INFO") as logs:
                    with self.assertRaises(expected_exception):
                        await CoreAgentService(_AgentModel(session)).answer(
                            question="test", local_context="", context=_context(),
                        )
                self.assertTrue(any(
                    f"status={status}" in line and "attempted_rounds=1" in line
                    and "unknown_token_rounds=1" in line for line in logs.output
                ))

    async def test_success_without_usage_is_not_recorded_as_known_free(self):
        session = _AgentSession([AgentStep("answer", (), AgentUsage())])
        with self.assertLogs("elbow_helper.features.agent.service", level="INFO") as logs:
            answer = await CoreAgentService(_AgentModel(session)).answer(
                question="test", local_context="", context=_context(),
            )
        self.assertEqual(answer, "answer")
        self.assertTrue(any(
            "status=completed" in line and "unknown_token_rounds=1" in line
            and "unknown_cache_rounds=1" in line for line in logs.output
        ))

    async def test_replayed_source_access_is_checked_before_first_model_round(self):
        context = _context()
        context.state.source_channels.add(200)
        session = _AgentSession([AgentStep("should not run", (), AgentUsage())])
        with patch("elbow_helper.features.agent.access.accessible_message_channel", return_value=None):
            with self.assertRaises(AgentAccessLost):
                await CoreAgentService(_AgentModel(session)).answer(
                    question="continue", local_context="", context=context,
                    conversation_history="Previously authorized evidence",
                )
        self.assertEqual(session.calls, [])

    async def test_revoked_evidence_never_reaches_a_second_model_round(self):
        context = _context()

        async def lookup(*args):
            context.state.source_channels.add(200)
            return {"private": "evidence"}

        tool = RegisteredAgentTool(AgentToolDefinition("lookup", "test", {"properties": {}}), lookup)
        session = _AgentSession([
            AgentStep("", (AgentToolCall("1", "lookup", "{}"),), AgentUsage()),
            AgentStep("should not run", (), AgentUsage()),
        ])
        with (
            patch("elbow_helper.features.agent.service.build_agent_tools", return_value={"lookup": tool}),
            patch("elbow_helper.features.agent.access.accessible_message_channel", return_value=None),
        ):
            with self.assertRaises(AgentAccessLost):
                await CoreAgentService(_AgentModel(session)).answer(
                    question="look up", local_context="", context=context,
                )
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(context.state.evidence, [])

    async def test_source_revoked_between_rounds_is_rechecked_even_after_successful_lookup(self):
        context = _context()
        context.state.source_channels.add(200)
        tool = RegisteredAgentTool(
            AgentToolDefinition("lookup", "test", {"properties": {}}),
            AsyncMock(return_value={"value": 7}),
        )
        session = _AgentSession([
            AgentStep("", (AgentToolCall("1", "lookup", "{}"),), AgentUsage()),
            AgentStep("should not run", (), AgentUsage()),
        ])
        # First round, before lookup, after lookup, then the next round.
        with (
            patch("elbow_helper.features.agent.service.build_agent_tools", return_value={"lookup": tool}),
            patch("elbow_helper.features.agent.access.accessible_message_channel",
                  side_effect=[object(), object(), object(), None]),
        ):
            with self.assertRaises(AgentAccessLost):
                await CoreAgentService(_AgentModel(session)).answer(
                    question="look up", local_context="", context=context,
                )
        self.assertEqual(len(session.calls), 1)
        tool.handler.assert_awaited_once()

    async def test_history_is_supplied_and_lookup_evidence_is_retained(self):
        context = _context()
        handler = AsyncMock(return_value={"answer": 7})
        tool = RegisteredAgentTool(AgentToolDefinition("lookup", "test", {"properties": {}}), handler)
        session = _AgentSession([
            AgentStep("", (AgentToolCall("1", "lookup", "{}"),), AgentUsage(prompt_tokens=100, completion_tokens=20)),
            AgentStep("done", (), AgentUsage(prompt_tokens=200, completion_tokens=30)),
        ])
        model = _AgentModel(session)
        with patch("elbow_helper.features.agent.service.build_agent_tools", return_value={"lookup": tool}):
            with self.assertLogs("elbow_helper.features.agent.service", level="INFO") as logs:
                await CoreAgentService(model).answer(question="continue", local_context="", context=context,
                                                     conversation_history="Earlier we discussed account #2PP")
        self.assertIn("Earlier we discussed account #2PP", model.request["prompt"])
        self.assertIn("answer", context.state.evidence[0])
        self.assertTrue(any("prompt_tokens=300 completion_tokens=50" in line and "status=completed" in line for line in logs.output))

    def test_arrays_booleans_and_enums_are_checked_before_tool_execution(self):
        schema = {"properties": {
            "roles": {"type": "array", "minItems": 1, "maxItems": 2, "uniqueItems": True, "items": {"type": "integer", "minimum": 1}},
            "refresh": {"type": "boolean"}, "selection": {"type": "string", "enum": ["all", "no_links"]},
        }}
        for arguments in ({"roles": []}, {"roles": [True]}, {"roles": [1, 1]}, {"roles": [1, 2, 3]},
                          {"roles": [-1]}, {"refresh": "false"}, {"selection": "delete"}):
            with self.subTest(arguments=arguments):
                self.assertFalse(_valid_arguments(arguments, schema))
        self.assertTrue(_valid_arguments({"roles": [1, 2], "refresh": False, "selection": "all"}, schema))

    def test_nested_object_arrays_are_validated_recursively(self):
        schema = {"properties": {"targets": {
            "type": "array", "minItems": 1, "maxItems": 2,
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "clan_code": {"type": "string", "enum": ["BEH", "BEC"]},
                    "slots": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["clan_code", "slots"],
            },
        }}, "required": ["targets"]}
        self.assertTrue(_valid_arguments({
            "targets": [{"clan_code": "BEH", "slots": 30}],
        }, schema))
        for arguments in (
            {"targets": [{"clan_code": "BEH"}]},
            {"targets": [{"clan_code": "OTHER", "slots": 30}]},
            {"targets": [{"clan_code": "BEH", "slots": True}]},
            {"targets": [{"clan_code": "BEH", "slots": 30, "sql": "x"}]},
            {"targets": ["BEH"]},
        ):
            with self.subTest(arguments=arguments):
                self.assertFalse(_valid_arguments(arguments, schema))

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
