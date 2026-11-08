"""Reply instructions and typed heading selection stay focused on the request."""

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from elbow_helper.agent.knowledge.store import KnowledgeCatalog, KnowledgeSection
from elbow_helper.agent.knowledge.tools import knowledge_tools
from tests.agent.engine import test_request_cost as cost
from tests.agent.engine.test_agent_plan_flow import _context, _model_step, _plan, _step


class PersonaBrevitySelectionTests(unittest.IsolatedAsyncioTestCase):
    flow = cost.RequestCostTests.flow

    async def test_model_selected_headings_exclude_automatic_and_adjacent_bodies(self):
        identity = KnowledgeSection("Family identity", "Brown Elbow is a Clash of Clans family.",
                                    file_name="family.md")
        returning = KnowledgeSection("Return workflow", "Returning members use the reactivation ticket.",
                                     file_name="return.md", always_loaded=True)
        unrelated = tuple(KnowledgeSection(
            f"Reference {index}", f"Unrequested reference body {index}.",
            file_name="reference.md", always_loaded=True,
        ) for index in range(6))
        private = KnowledgeSection("Private review", "Restricted review contents.",
                                   visibility="examiner", file_name="private.md", always_loaded=True)
        catalog = KnowledgeCatalog("synthetic", (identity, returning, *unrelated, private)).prepare_prompts()
        registry = {tool.definition.name: tool for tool in knowledge_tools()}

        for selected in ((identity,), (identity, returning)):
            with self.subTest(titles=[section.title for section in selected]):
                context = replace(_context(), knowledge_store=SimpleNamespace(load=lambda: catalog))
                reply = "\n".join(section.body for section in selected)
                flow, session, model, _ = self.flow([
                    _model_step(_plan([{
                        **_step("answer", {"titles": [section.title for section in selected]}),
                        "capability": "search_approved_knowledge",
                    }])),
                    cost.response(reply),
                ], registry=registry, context=context, question=(
                    "What is Brown Elbow?" if len(selected) == 1
                    else "What is Brown Elbow, and how do returning members reactivate?"
                ))
                prompt = model.request["system_prompt"]
                for section in catalog.active_sections:
                    self.assertNotIn(section.body, prompt)
                    self.assertNotIn(section.body, flow.answer_context)
                self.assertIn("- Family identity", model.request['prompt'])
                self.assertIn("- Return workflow", model.request['prompt'])
                self.assertNotIn(private.title, prompt)
                self.assertFalse(context.state.knowledge_core_available)
                # Exact model-selected headings require no lexical request matching.
                with patch.object(KnowledgeCatalog, "search", side_effect=AssertionError("Unexpected lexical search")):
                    self.assertEqual(await flow.run(), reply)
                result = json.loads(session.calls[-1][0][0].content)["results"]["answer"]
                self.assertEqual([section["body"] for section in result["sections"]],
                                 [section.body for section in selected])
                self.assertEqual(len(context.state.knowledge_sources), len(selected))
                self.assertEqual(flow.runner.tool_calls, 1)
                self.assertEqual(len(session.calls), 2)


if __name__ == "__main__":
    unittest.main()
