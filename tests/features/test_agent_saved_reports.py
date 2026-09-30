"""One saved-report surface keeps each report's own read rules."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.tools import build_agent_tool_groups, build_agent_tools
from elbow_helper.features.agent.tools import saved_reports
from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.infrastructure.ai import AgentToolDefinition


class SavedReportRegistryTests(unittest.TestCase):
    def test_every_retained_reader_and_comparer_uses_generic_surface(self):
        original = {
            tool.definition.name
            for group in build_agent_tool_groups().values() for tool in group
        }
        readers = {
            name for name in original
            if name.startswith("read_") and (name.endswith("_report") or name.endswith("_import"))
        }
        comparers = {
            name for name in original
            if name.startswith("compare_") and name.endswith("_reports")
        }
        self.assertEqual(set(saved_reports.REPORT_READERS.values()), readers)
        self.assertEqual(set(saved_reports.REPORT_COMPARERS.values()), comparers)
        registry = build_agent_tools()
        self.assertTrue(readers.isdisjoint(registry))
        self.assertTrue(comparers.isdisjoint(registry))
        self.assertEqual(set(registry) & {saved_reports.READ_NAME, saved_reports.COMPARE_NAME},
                         {saved_reports.READ_NAME, saved_reports.COMPARE_NAME})
        for generic, kinds in ((saved_reports.READ_NAME, saved_reports.REPORT_READERS),
                               (saved_reports.COMPARE_NAME, saved_reports.REPORT_COMPARERS)):
            self.assertEqual(
                set(registry[generic].definition.parameters["properties"]["report_kind"]["enum"]),
                set(kinds),
            )


class SavedReportRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_uses_selected_handler_only_for_matching_kind(self):
        handler = AsyncMock(return_value={"rows": [1]})
        original = RegisteredAgentTool(AgentToolDefinition(
            name="read_synthetic_report", description="Read a synthetic report.",
            parameters={"type": "object", "properties": {
                "report_id": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
            }, "required": ["report_id"], "additionalProperties": False},
        ), handler)
        surface = saved_reports._tool(saved_reports.READ_NAME, {"synthetic": original})
        state = SimpleNamespace(reports={"one": SimpleNamespace(
            manifest=lambda: {"kind": "synthetic"},
        )})
        context = SimpleNamespace(state=state)
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
            ), handler)
            return saved_reports._tool(saved_reports.READ_NAME, {"synthetic": tool})
        context = SimpleNamespace(state=SimpleNamespace(reports={"one": SimpleNamespace(
            manifest=lambda: {"kind": "synthetic"},
        )}))
        earlier = surface(first)
        later = surface(second)
        arguments = {"report_kind": "synthetic", "report_id": "one"}
        self.assertEqual(await earlier.handler(context, arguments), {"source": "first"})
        self.assertEqual(await later.handler(context, arguments), {"source": "second"})
