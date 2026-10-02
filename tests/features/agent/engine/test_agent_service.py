from __future__ import annotations

from features.agent.engine.helpers import patch_engine

import asyncio
from dataclasses import replace
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
import unittest
import json
import time
from itertools import chain, repeat
import discord
from unittest.mock import AsyncMock
from unittest.mock import patch

from elbow_helper.features.agent.models import AgentAttachment, AgentCapabilityEffect, RegisteredAgentTool
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.configuration.roles import CORE, LEAD, LEAD_PLUS
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.engine.capability_contract import compile_capability_call
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.plan.checker import valid_arguments
from elbow_helper.features.agent.engine.tool_call import bound_tool_result as _bound_tool_result
from elbow_helper.features.agent.engine.tool_call import evidence_record as _evidence_record
from elbow_helper.features.agent.access import ACCESS_LEAD_PLUS, AgentAccessLost
from elbow_helper.features.agent.capabilities.account_links.role_report import RoleAccountReport
from elbow_helper.infrastructure.ai import AgentStep
from elbow_helper.infrastructure.ai import AgentToolCall
from elbow_helper.infrastructure.ai import AgentToolDefinition
from elbow_helper.infrastructure.ai import AgentUsage
from elbow_helper.infrastructure.ai import TextGenerationError
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from elbow_helper.features.agent.engine.service import AgentUnavailableError
from tests.features.agent_plan_helpers import plan_call


class _AgentSession:
    def __init__(self, steps: list[AgentStep]):
        self.steps = steps
        self.calls: list[tuple[tuple[object, ...], bool]] = []
        self.efforts: list[AgentReasoningEffort] = []
        self.output_limits: list[int | None] = []
        self.tool_replacements = []

    def replace_tools(self, tools):
        self.tool_replacements.append(tuple(tools))

    async def advance(self, tool_results=(), *, allow_tools=True,
                      reasoning_effort=None, max_output_tokens=None):
        self.calls.append((tuple(tool_results), allow_tools))
        self.efforts.append(reasoning_effort)
        self.output_limits.append(max_output_tokens)
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
    channel = SimpleNamespace(
        id=100, type=discord.ChannelType.text, overwrites={},
        permissions_for=lambda member: SimpleNamespace(view_channel=True, read_message_history=True),
    )
    message = SimpleNamespace(
        channel=channel,
        created_at=datetime(2026, 9, 14, tzinfo=timezone.utc),
    )
    default_role = SimpleNamespace(id=1)
    guild = SimpleNamespace(
        id=1, name="Brown Elbow", me=member,
        default_role=default_role, roles=[default_role, *member.roles],
        get_member=lambda member_id: member,
    )
    channel.guild = guild
    guild.get_channel_or_thread = lambda channel_id: channel if channel_id == 100 else None
    return AgentRequestContext(
        bot=None, account_links=None, clan_health=None, message_search=None,
        member=member,
        source_message=message,
        guild=guild,
    )


class AgentServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_static_role_contract_blocks_handler_and_public_disclosure(self):
        tool = build_agent_tools()["read_role_connections"]
        scope = compile_capability_call(tool, {})
        self.assertEqual(scope["required_access"], ("lead",))
        handler = AsyncMock(return_value={"rules": ["private"]})
        context = _context()

        with self.assertRaises(AgentAccessLost):
            await AgentService.execute_tool(
                name="read_role_connections", handler=handler, arguments={},
                capability_scope=scope, context=context,
            )
        handler.assert_not_awaited()
        self.assertEqual(context.state.required_access, set())

        lead_role = SimpleNamespace(id=next(iter(LEAD)))
        context.member.roles.append(lead_role)
        context.guild.roles.append(lead_role)
        result = await AgentService.execute_tool(
            name="read_role_connections", handler=handler, arguments={},
            capability_scope=scope, context=context,
        )
        handler.assert_awaited_once()
        self.assertIn("error", json.loads(result))
        self.assertEqual(context.state.required_access, set())

    async def test_static_role_contract_marks_retained_report_without_handler_help(self):
        tool = build_agent_tools()["read_role_connections"]
        scope = compile_capability_call(tool, {})
        context = _context()
        lead_role = SimpleNamespace(id=next(iter(LEAD)))
        context.member.roles.append(lead_role)
        context.guild.roles.append(lead_role)
        context.source_message.channel.permissions_for = lambda actor: SimpleNamespace(
            view_channel=getattr(actor, "id", None) in {context.member.id, lead_role.id},
            read_message_history=True,
        )
        report = RoleAccountReport("role-report", "2026-09-17", (), ())

        async def handler(request_context, _arguments):
            request_context.state.reports[report.report_id] = report
            return {"report_id": report.report_id}

        result = await AgentService.execute_tool(
            name="read_role_connections", handler=handler, arguments={},
            capability_scope=scope, context=context,
        )
        self.assertEqual(json.loads(result), {"report_id": report.report_id})
        self.assertEqual(context.state.required_access, {"lead"})
        self.assertEqual(
            context.state.report_access_requirements[report.report_id],
            frozenset({"lead"}),
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
            "elbow_helper.features.agent.engine.tool_call", level="ERROR",
        ):
            result = await AgentService.execute_tool(
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
            "elbow_helper.features.agent.engine.tool_call.require_evidence_access",
            new=AsyncMock(side_effect=AgentAccessLost("lost")),
        ):
            with self.assertRaises(AgentAccessLost):
                await AgentService.execute_tool(
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
            await AgentService.execute_tool(
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
            await AgentService.execute_tool(
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

        async def lookup(request_context, _):
            request_context.state.reports[report.report_id] = report
            return {"report_id": report.report_id}

        tool = RegisteredAgentTool(
            AgentToolDefinition("lookup", "test", {"properties": {}}), lookup,
        )
        session = _AgentSession([
            AgentStep("", (plan_call(AgentToolCall("1", "lookup", "{}")),), AgentUsage()),
            AgentStep("done", (), AgentUsage()),
        ])
        with (
            patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value={"lookup": tool}),
            patch("elbow_helper.features.agent.access.accessible_message_channel", return_value=object()),
            patch_engine("require_disclosure_access"),
            patch("elbow_helper.features.agent.engine.tool_call.require_destination_access"),
        ):
            await AgentService(_AgentModel(session)).answer(
                question="test", local_context="", context=context,
            )
        self.assertEqual(context.state.report_sources["report"], frozenset({100, 200}))
        self.assertEqual(
            context.state.report_access_requirements["report"],
            frozenset({ACCESS_LEAD_PLUS}),
        )

    def test_evidence_record_marks_bounded_model_view_as_partial(self):
        context = _context()
        context.state.source_channels.update({100, 200})
        context.state.required_access.add(ACCESS_LEAD_PLUS)
        record = json.loads(_evidence_record(
            call_id="call", tool="lookup", arguments={"period": "2026-09"},
            result='{"truncated":true}', raw_result_characters=9000,
            result_complete=False, context=context,
        ))
        self.assertEqual(record["result_status"], "partial")
        self.assertFalse(record["result_complete"])
        self.assertEqual(record["source_channels"], [100, 200])
        self.assertEqual(record["required_access"], [ACCESS_LEAD_PLUS])
        self.assertEqual(record["raw_result_characters"], 9000)

    def test_arrays_booleans_and_enums_are_checked_before_tool_execution(self):
        schema = {"properties": {
            "roles": {"type": "array", "minItems": 1, "maxItems": 2, "uniqueItems": True, "items": {"type": "integer", "minimum": 1}},
            "refresh": {"type": "boolean"}, "selection": {"type": "string", "enum": ["all", "no_links"]},
        }}
        for arguments in ({"roles": []}, {"roles": [True]}, {"roles": [1, 1]}, {"roles": [1, 2, 3]},
                          {"roles": [-1]}, {"refresh": "false"}, {"selection": "delete"}):
            with self.subTest(arguments=arguments):
                self.assertFalse(valid_arguments(arguments, schema))
        self.assertTrue(valid_arguments({"roles": [1, 2], "refresh": False, "selection": "all"}, schema))

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
        self.assertTrue(valid_arguments({
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
                self.assertFalse(valid_arguments(arguments, schema))

    def test_schema_rejects_missing_unknown_and_wrongly_typed_arguments(self):
        schema = {"properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["limit"]}
        for arguments in ({}, {"limit": True}, {"limit": "3"}, {"limit": 11}, {"limit": 3, "sql": "anything"}):
            with self.subTest(arguments=arguments):
                self.assertFalse(valid_arguments(arguments, schema))
        self.assertTrue(valid_arguments({"limit": 3}, schema))

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

        answer = await AgentService(model).answer(
            question="tell this guy to piss off",
            local_context="Message directly replied to: he asked for another reminder",
            context=_context(),
        )

        self.assertEqual(answer, "He said what he said. Leave the man alone.")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][0], ())
        self.assertIn("tell this guy to piss off", model.request["prompt"])
        self.assertIn("he asked for another reminder", model.request["prompt"])
