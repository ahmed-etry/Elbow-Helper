"""Scripted cost reductions keep the same outputs without exhausting research."""

from copy import deepcopy
from features.agent.engine.helpers import LegacyCatalogueMixin

import json
import unittest

from features.agent.engine.efficiency_replay import inspect_record
from features.agent.engine.efficiency_scenarios import measure


class EfficiencyScenarioTests(LegacyCatalogueMixin, unittest.IsolatedAsyncioTestCase):
    async def test_workloads_finish_in_three_rounds_with_identical_answers(self):
        report = await measure()
        self.assertEqual(set(report["cases"]), {"accounts", "recruitment", "standing", "refusal", "be4_cohort", "clan_ass"})
        for name, result in report["cases"].items():
            with self.subTest(name=name):
                self.assertLessEqual(result["after"]["rounds"], 3)
                self.assertLess(result["after"]["prompt_tokens_estimated"], 80_000)
                self.assertLess(result["after"]["prompt_tokens_estimated"], result["before"]["prompt_tokens_estimated"])
        self.assertEqual(report["cases"]["clan_ass"]["after"]["rounds"], 3)

    def test_archive_replay_counts_only_result_savings_without_changing_rounds_or_source(self):
        section = {"section_id": "synthetic", "body": "Policy evidence. " * 500}
        payload = json.dumps({"results": {"rules": {"sections": [section]}}})
        record = {"usage": {"prompt_tokens": 60_000, "completion_tokens": 200}, "model_rounds": 3,
                  "tool_calls": [], "model_sessions": [{"tools": ["plan"], "rounds": [
                      {"usage": {"prompt_tokens": 20_000}, "tool_results": [{"content": payload}]},
                      {"usage": {"prompt_tokens": 20_000}, "tool_results": [{"content": payload}]},
                      {"usage": {"prompt_tokens": 20_000}, "tool_results": []},
                  ]}]}
        original = deepcopy(record)
        report = inspect_record(record)
        replay = report["unchanged_schedule_replay"]
        self.assertEqual(replay["rounds"], record["model_rounds"])
        self.assertLess(replay["prompt_tokens_estimated"], report["observed"]["prompt_tokens"])
        self.assertLess(replay["result_characters_after"], replay["result_characters_before"])
        self.assertEqual(record, original)
