import unittest

from elbow_helper.features.agent.budgets import ContextBudget
from elbow_helper.infrastructure.ai import AgentToolResult, AgentUsage


class ContextBudgetTests(unittest.TestCase):
    def test_output_room_is_reserved_without_reducing_output_allowance(self):
        budget = ContextBudget(1000, 200, 500)
        self.assertTrue(budget.can_answer(800))
        self.assertFalse(budget.can_answer(801))
        self.assertTrue(budget.can_continue_tools(500, result_reserve=100))
        self.assertFalse(budget.can_continue_tools(501, result_reserve=100))
        self.assertEqual(budget.output_reserve, 200)

    def test_missing_usage_reserves_full_completion_allowance(self):
        budget = ContextBudget(1000, 200, 100)
        budget.observe(AgentUsage(), projected_input=100)
        self.assertEqual(budget.next_input_estimate, 428)

    def test_observed_usage_replaces_estimate_and_zero_is_not_missing(self):
        budget = ContextBudget(1000, 200, 500)
        budget.observe(AgentUsage(prompt_tokens=50, completion_tokens=0), projected_input=500)
        self.assertEqual(budget.next_input_estimate, 178)

    def test_tool_results_include_content_and_framing_in_next_input(self):
        budget = ContextBudget(1000, 200, 100)
        projected = budget.projected_input((AgentToolResult("call", "界" * 100),))
        self.assertGreater(projected, 400)
        self.assertEqual(budget.projected_input(()), 100)

    def test_unknown_model_capacity_does_not_invent_a_limit(self):
        budget = ContextBudget(None, 200, 100)
        self.assertTrue(budget.can_answer(10_000_000))
        self.assertTrue(budget.can_continue_tools(10_000_000, result_reserve=500))

    def test_batch_results_share_context_room_and_reserve_a_final_answer(self):
        budget = ContextBudget(20_000, 4000, 8000)
        first_limit = budget.result_character_limit((), pending_call_ids=("one", "two"), maximum=10_000)
        self.assertGreater(first_limit, 0)
        first = AgentToolResult("one", "\U0001f600" * first_limit)
        second_limit = budget.result_character_limit((first,), pending_call_ids=("two",), maximum=10_000)
        self.assertLess(second_limit, first_limit)
        second = AgentToolResult("two", "\U0001f600" * second_limit)
        self.assertTrue(budget.can_answer(budget.projected_input((first, second))))

    def test_no_result_room_returns_zero(self):
        budget = ContextBudget(1000, 200, 800)
        self.assertEqual(budget.result_character_limit((), pending_call_ids=("one",), maximum=100), 0)
