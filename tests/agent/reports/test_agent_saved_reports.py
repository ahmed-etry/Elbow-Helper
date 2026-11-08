"""One saved-report surface keeps each report's own read rules."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.agent.reports import tools as saved_reports
from elbow_helper.agent.engine.models import RegisteredAgentTool
from elbow_helper.agent.engine.capability_contract import CapabilityContract
from elbow_helper.infrastructure.ai import AgentToolDefinition
from tests.agent.engine.test_agent_plan_flow import _context


class SavedReportRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_uses_selected_handler_only_for_matching_kind(self):
        handler = AsyncMock(return_value={"rows": [1]})
        original = RegisteredAgentTool(AgentToolDefinition(
            name="read_synthetic_report", description="Read a synthetic report.",
            parameters={"type": "object", "properties": {
                "report_id": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
            }, "required": ["report_id"], "additionalProperties": False},
        ), handler, contract=CapabilityContract(
            entity_fields=(("report_id", "synthetic_report"),), retained_fields=("report_id",),
        ))
        surface = saved_reports._tool(saved_reports.READ_NAME, {"synthetic": original})
        state = SimpleNamespace(reports={"one": SimpleNamespace(
            manifest=lambda: {"kind": "synthetic"},
        )})
        context = _context()
        context.state.reports.update(state.reports)
        context.state.report_sources["one"] = frozenset({91})
        context.state.report_access_requirements["one"] = frozenset()
        result = await surface.handler(context, {
            "report_kind": "synthetic", "report_id": "one", "offset": 2,
        })
        self.assertEqual(result, {"rows": [1]})
        handler.assert_awaited_once_with(context, {"report_id": "one", "offset": 2})
        result = await surface.handler(context, {
            "report_kind": "synthetic", "report_id": "missing",
        })
        self.assertIn("error", result)
        result = await surface.handler(context, {
            "report_kind": "synthetic", "report_id": "one", "limit": 2,
        })
        self.assertIn("offset", result["error"])
        handler.assert_awaited_once()

    async def test_report_handlers_do_not_cross_registry_builds(self):
        first = AsyncMock(return_value={"source": "first"})
        second = AsyncMock(return_value={"source": "second"})
        def surface(handler):
            tool = RegisteredAgentTool(AgentToolDefinition(
                name="read_synthetic_report", description="Read a synthetic report.",
                parameters={"type": "object", "properties": {
                    "report_id": {"type": "string"},
                }, "required": ["report_id"], "additionalProperties": False},
            ), handler, contract=CapabilityContract(
                entity_fields=(("report_id", "synthetic_report"),), retained_fields=("report_id",),
            ))
            return saved_reports._tool(saved_reports.READ_NAME, {"synthetic": tool})
        context = _context()
        context.state.reports["one"] = SimpleNamespace(manifest=lambda: {"kind": "synthetic"})
        context.state.report_sources["one"] = frozenset({91})
        context.state.report_access_requirements["one"] = frozenset()
        earlier = surface(first)
        later = surface(second)
        arguments = {"report_kind": "synthetic", "report_id": "one"}
        self.assertEqual(await earlier.handler(context, arguments), {"source": "first"})
        self.assertEqual(await later.handler(context, arguments), {"source": "second"})
