from __future__ import annotations

import asyncio

from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
import unittest
import json

import discord
from unittest.mock import AsyncMock
from unittest.mock import patch

from elbow_helper.agent.engine.models import AgentAttachment
from elbow_helper.agent.engine.models import AgentRequestContext
from elbow_helper.configuration.roles import CORE, LEAD, LEAD_PLUS
from elbow_helper.agent.engine.tool_call import execute_tool
from elbow_helper.agent.engine.capability_contract import compile_capability_call
from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.plan.checker import valid_arguments
from elbow_helper.agent.safety.access import ACCESS_LEAD_PLUS, AgentAccessLost
from tests.agent.reports.report_helpers import make_event_report






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
        id=1, name="Brown Elbow", me=SimpleNamespace(id=999, bot=True, roles=[]), chunked=True, members=[member],
        default_role=default_role, roles=[default_role, *member.roles],
        get_member=lambda member_id: member,
    )
    channel.guild = guild
    guild.get_channel_or_thread = lambda channel_id: channel if channel_id == 100 else None
    return AgentRequestContext(
        bot=None, account_links=None, message_search=None,
        member=member,
        source_message=message,
        guild=guild,
    )


class ToolCallTests(unittest.IsolatedAsyncioTestCase):


    async def test_lookup_marks_only_its_own_new_private_candidate(self):
        context = _context()

        async def refused(context, arguments):
            context.state.scope_audit.private_candidates.add('synthetic-source')
            return {'error': 'Synthetic restricted source.', 'cause': 'access_denied'}

        result = json.loads(await execute_tool(
            name='synthetic_read', handler=refused, arguments={}, context=context,
        ))
        self.assertTrue(result['available_privately'])

    async def test_static_role_contract_blocks_handler_and_public_disclosure(self):
        tool = build_agent_tools()["read_role_connections"]
        scope = {**compile_capability_call(tool, {}), "required_access": ("lead",)}
        self.assertEqual(scope["required_access"], ("lead",))
        handler = AsyncMock(return_value={"rules": ["private"]})
        context = _context()

        result = await execute_tool(
            name="read_role_connections", handler=handler, arguments={},
            capability_scope=scope, context=context,
        )
        self.assertIn("error", json.loads(result))
        handler.assert_not_awaited()
        self.assertEqual(context.state.required_access, set())

        lead_role = SimpleNamespace(id=next(iter(LEAD)))
        context.member.roles.append(lead_role)
        context.guild.roles.append(lead_role)
        context.state.scope = None
        result = await execute_tool(
            name="read_role_connections", handler=handler, arguments={},
            capability_scope=scope, context=context,
        )
        handler.assert_awaited_once()
        self.assertNotIn("error", json.loads(result))
        self.assertEqual(context.state.required_access, {"lead"})


    async def test_failed_tool_restores_request_local_state_exactly(self):
        context = _context()
        context.member.roles.append(SimpleNamespace(id=next(iter(LEAD_PLUS))))
        original = make_event_report("original", "2026-09-17")
        replacement = make_event_report("replacement", "2026-09-18")
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
            "elbow_helper.agent.engine.tool_call", level="ERROR",
        ):
            result = await execute_tool(
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
        original = make_event_report("original", "2026-09-17")
        replacement = make_event_report("replacement", "2026-09-18")
        context.state.reports[original.report_id] = original
        context.state.report_sources[original.report_id] = frozenset({100})
        context.state.report_access_requirements[original.report_id] = frozenset()

        async def lookup(*_):
            context.state.reports.clear()
            context.state.reports[replacement.report_id] = replacement
            return {"report_id": replacement.report_id}
        with patch(
            "elbow_helper.agent.engine.tool_call.require_evidence_access",
            new=AsyncMock(side_effect=AgentAccessLost("lost")),
        ):
            with self.assertRaises(AgentAccessLost):
                await execute_tool(
                    name="lookup", handler=lookup, arguments={}, context=context,
                )
        self.assertEqual(context.state.reports, {"original": original})
        self.assertEqual(
            context.state.report_sources, {"original": frozenset({100})},
        )

    async def test_cancelled_tool_rolls_back_before_propagating(self):
        context = _context()
        original = make_event_report("original", "2026-09-17")
        context.state.reports[original.report_id] = original

        async def cancelled(*_):
            context.state.reports.clear()
            context.state.attachments.append(AgentAttachment("partial.xlsx", b"x"))
            raise asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await execute_tool(
                name="cancelled", handler=cancelled, arguments={}, context=context,
            )
        self.assertEqual(context.state.reports, {"original": original})
        self.assertEqual(context.state.attachments, [])



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
