"""Author searches preserve scope through planning and execution."""
import unittest

from features.agent.research.test_agent_search import _context
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.plan.checker import check_plan, check_step
from elbow_helper.features.agent.plan.executor import execute_plan
from elbow_helper.features.agent.plan.results import plan_feedback
from elbow_helper.features.agent.research.search import search_discord_messages


def author_plan(arguments):
    return {"goal": "Read a member's accessible history", "effort": "low", "output": "text",
            "steps": [
                {"id": "member", "capability": "find_discord_members",
                 "arguments": {"query": "synthetic-member"}, "reason": "Resolve member",
                 "depends_on": []},
                {"id": "history", "capability": "search_discord_messages",
                 "arguments": {"author_id": {"step": "member", "path": ["members", 0, "member_id"]},
                               **arguments},
                 "reason": "Read history", "depends_on": ["member"]},
            ]}


class SearchContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = build_agent_tools()

    def test_author_search_does_not_require_keywords_or_invented_dates(self):
        for arguments in ({}, {"query": ""}, {"after": "2026-01-01"},
                          {"before": "2026-02-01"}):
            with self.subTest(arguments=arguments):
                result = check_plan(author_plan(arguments), self.registry)
                self.assertTrue(result.ok, result.error)



    def test_feedback_identifies_each_invalid_argument_without_echoing_values(self):
        plan = author_plan({"limit": 0, "query": 123})
        result = check_plan(plan, self.registry)
        self.assertTrue(result.ok)
        feedback = plan_feedback(result.step_errors["history"], step_id="history")
        self.assertIn("limit must match", feedback["error"])
        self.assertIn("query must match", feedback["error"])
        self.assertNotIn("123", feedback["error"])
        self.assertEqual(feedback["step_id"], "history")


    async def test_author_reference_reaches_search_without_a_keyword_filter(self):
        context = _context()
        plan = author_plan({"query": "", "limit": 25})
        self.assertTrue(check_plan(plan, self.registry).ok)

        async def run(step, arguments, results):
            if step["id"] == "member":
                return {"members": [{"member_id": 1}]}
            checked = check_step({**step, "arguments": arguments}, self.registry, resolved=True)
            self.assertTrue(checked.ok, checked.error)
            return await search_discord_messages(context, arguments)

        results = await execute_plan(plan, run)
        self.assertNotIn("error", results["history"])
        context.message_search.search.assert_awaited_once()
        values = context.message_search.search.await_args.kwargs
        self.assertEqual(values["author_id"], 1)
        self.assertEqual(values["content"], "")
        self.assertEqual(values["channel_ids"], (100, 200))

    async def test_empty_unfiltered_search_does_not_fetch_server_history(self):
        context = _context()
        result = await search_discord_messages(context, {"query": ""})
        self.assertIn("error", result)
        context.message_search.search.assert_not_awaited()
