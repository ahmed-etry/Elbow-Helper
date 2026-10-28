"""Reply instructions and typed heading selection stay focused on the request."""

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from elbow_helper.agent.knowledge.store import KnowledgeCatalog, KnowledgeSection
from elbow_helper.agent.knowledge.tools import knowledge_tools
from elbow_helper.agent.plan.format import system_instructions
from elbow_helper.agent.prompts import RESEARCH_RULES
from agent.engine import test_request_cost as cost
from agent.engine.test_agent_plan_flow import _context, _model_step, _plan, _step


class PersonaBrevityInstructionTests(unittest.TestCase):
    def test_answer_contract_reaches_read_only_and_action_modes(self):
        for actions in (False, True):
            prompt = system_instructions({}, actions_enabled=actions)
            for rule in (
                "Lead with the fact that settles the question",
                "Casual factual questions get 1-3 short human lines",
                "omit side facts and caveats unless they change the answer",
                "Do not repeat the question's framing as an introduction",
                "Each stated fact must be traceable to a source or data read supporting that exact claim",
                "Never synthesize a community rule, exception or workflow from the question plus a knowledge line",
                '"nb only" means the bare number',
                '"stay concise" is a hard brevity constraint',
                "return only that output: no opener, apology, caption, joke or closer",
                "Brevity must not remove requested members, accounts or rows",
                "never aligned code-block tables",
                "Attach files only when requested or when the requested output rows cannot fit a message",
                "put the verified main account first, then descending Town Hall or rank",
                "Never default to storage-key or player-tag order",
                "Do not pad replies with successful-read notices",
                "test-only roles",
            ):
                with self.subTest(actions=actions, rule=rule):
                    self.assertIn(rule, prompt)
            social = system_instructions({}, actions_enabled=actions, social=True)
            for rule in ("Check that the joke's actors and actions fit the supplied exchange",
                         "celebrate the win directly; do not undercut it"):
                self.assertIn(rule, social)
                self.assertNotIn(rule, prompt)

    def test_selection_instructions_require_relevance_before_opening_sections(self):
        for rule in (
            "Select the smallest set of knowledge headings needed to answer the request",
            "single best heading for a simple fact or definition",
            "distinct requested part or a prerequisite that can change the answer",
            "The title and page limits are ceilings, not targets",
            "Stop reading when\nthe requested answer is supported",
            "never regex or word lists over member-written text",
            "an index title alone\nis not the section's contents",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, RESEARCH_RULES)


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
