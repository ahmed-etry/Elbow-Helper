"""Synthetic comparisons resolve event participation before historical data hunts."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.knowledge.store import KnowledgeCatalog, KnowledgeSection
from elbow_helper.features.agent.knowledge.tools import knowledge_tools
from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.infrastructure.ai import AgentToolDefinition
from features.agent.engine import test_request_cost as cost
from features.agent.engine.test_agent_plan_flow import _context, _model_step, _plan, _step


class KnowledgeFirstComparisonTests(unittest.IsolatedAsyncioTestCase):
    flow = cost.RequestCostTests.flow

    async def test_stopped_event_needs_one_knowledge_read_and_no_history_hunt(self):
        catalog = KnowledgeCatalog("now", (
            KnowledgeSection("Hellbow (BEH)", "Hellbow stopped CWL after March 2026.",
                             file_name="clan-profiles.md"),
            KnowledgeSection("Brown Elbow IV (BE4)", "BE4 is the main CWL clan.",
                             file_name="clan-profiles.md"),
        )).prepare_prompts()
        context = replace(_context(), knowledge_store=SimpleNamespace(load=lambda: catalog))
        history = AsyncMock(return_value={"rows": []})
        registry = {tool.definition.name: tool for tool in knowledge_tools()}
        registry["query_bot_data"] = RegisteredAgentTool(AgentToolDefinition(
            "query_bot_data", "Read historical seasons.", {"type": "object", "properties": {}},
        ), history)
        final = ("Hellbow stopped CWL after March 2026; BE4 is the main CWL clan. "
                 "A recent three-season BEH clan comparison doesn't apply. "
                 "I can compare BE4's seasons or BEH members' CWL in other clans.")
        flow, session, model, _ = self.flow([
            _model_step(_plan([{**_step("profiles", {"titles": [
                "Hellbow (BEH)", "Brown Elbow IV (BE4)",
            ]}), "capability": "search_approved_knowledge"}])),
            cost.response(final),
        ], question="Compare BEH's CWL reliability over three recent seasons.",
            registry=registry, context=context)
        self.assertIn("clan profile and event", model.request["system_prompt"])
        self.assertEqual(await flow.run(), final)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(flow.runner.tool_calls, 1)
        history.assert_not_awaited()
        results = session.calls[-1][0][0].content
        self.assertIn("Hellbow stopped CWL after March 2026", results)
        self.assertIn("BE4 is the main CWL clan", results)
        self.assertIn("use the relevant clan profile and event policy from knowledge", results)

    async def test_always_loaded_profile_is_opened_once_before_comparison(self):
        catalog = KnowledgeCatalog("now", (
            KnowledgeSection("Hellbow (BEH)", "Hellbow stopped CWL; BE4 is the main CWL clan.",
                             file_name="clan-profiles.md", always_loaded=True),
        )).prepare_prompts()
        context = replace(_context(), knowledge_store=SimpleNamespace(load=lambda: catalog))
        final = "Hellbow stopped CWL; BE4 is the main CWL clan. I can compare BE4's seasons."
        registry = {tool.definition.name: tool for tool in knowledge_tools()}
        flow, session, model, _ = self.flow([
            _model_step(_plan([{**_step("profile", {"titles": ["Hellbow (BEH)"]}),
                               "capability": "search_approved_knowledge"}])),
            cost.response(final),
        ], context=context, registry=registry,
            question="Compare BEH's CWL reliability over three seasons.")
        self.assertIn("- Hellbow (BEH)", model.request["prompt"])
        self.assertNotIn(catalog.active_sections[0].body, model.request["system_prompt"])
        self.assertEqual(await flow.run(), final)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(flow.runner.tool_calls, 1)
        self.assertIn(catalog.active_sections[0].body, session.calls[-1][0][0].content)
