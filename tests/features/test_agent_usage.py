import unittest

from elbow_helper.features.agent.usage import RequestUsage
from elbow_helper.infrastructure.ai import AgentUsage
from elbow_helper.infrastructure.ai.client import _usage_value


class RequestUsageTests(unittest.TestCase):
    def test_rounds_sum_without_double_counting_cached_input(self):
        usage = RequestUsage(attempted_rounds=2)
        usage.observe(AgentUsage(100, 20, 80, 20))
        usage.observe(AgentUsage(150, 30, 120, 30))
        self.assertEqual((usage.prompt_tokens, usage.completion_tokens), (250, 50))
        self.assertEqual((usage.cache_hit_tokens, usage.cache_miss_tokens), (200, 50))
        self.assertEqual((usage.unknown_token_rounds, usage.unknown_cache_rounds), (0, 0))

    def test_missing_usage_and_unreturned_round_are_unknown(self):
        usage = RequestUsage(attempted_rounds=3)
        usage.observe(AgentUsage(100, 20, 80, 20))
        usage.observe(AgentUsage())
        self.assertEqual(usage.prompt_tokens, 100)
        self.assertEqual((usage.unknown_token_rounds, usage.unknown_cache_rounds), (2, 2))

    def test_missing_cache_breakdown_does_not_invalidate_total_tokens(self):
        usage = RequestUsage(attempted_rounds=1)
        usage.observe(AgentUsage(100, 0))
        self.assertEqual((usage.unknown_token_rounds, usage.unknown_cache_rounds), (0, 1))

    def test_zero_is_reported_and_invalid_usage_is_not(self):
        self.assertEqual(_usage_value({'tokens': 0}, 'tokens'), 0)
        self.assertEqual(_usage_value({'tokens': 123}, 'tokens'), 123)
        for value in (None, True, False, -1, 1.5, '123', 'bad', float('inf')):
            with self.subTest(value=value):
                self.assertIsNone(_usage_value({'tokens': value}, 'tokens'))
        self.assertIsNone(_usage_value(None, 'tokens'))
