"""Failed reads must not poison corrected evidence or terminate recovery."""

from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.engine.factual_answers import narrow_answer
from elbow_helper.features.agent.engine.grounding import GroundingError
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.models import RegisteredAgentTool
from elbow_helper.infrastructure.ai import AgentStep, AgentToolDefinition, AgentUsage
from features.agent.engine.test_agent_plan_flow import _Model, _Session, _context, _model_step, _plan
from features.agent.engine.test_factual_answers import sample
from features.agent.engine.test_strict_answers import players, query


class AnswerRecoveryTests(unittest.TestCase):
    def test_failed_observations_do_not_compete_with_successful_answers(self):
        for question, good, expected in (
            ("names only", players(), "North\nSouth"),
            ("tags only", players(), "#P0\n#P2"),
            ("count only", query([{"count": 2}]), "2"),
            ("names only", query([{"name": "North"}]), "North"),
            ("Average stars for ranked defenses?", sample(), "Average stars: 2.3333"),
        ):
            with self.subTest(question=question, source=good.get("source")):
                failed = {**deepcopy(good), "error": "The earlier lookup failed.",
                          "flags": {"status": "failed"}}
                answer = narrow_answer(question, {"failed": failed, "corrected": good})
                self.assertTrue(answer.startswith(expected))

    def test_no_successful_clash_items_do_not_poison_corrected_names(self):
        failed = {"source": "clash_api", "kind": "player", "items": [
            {"tag": "#P0", "status": "not_found"}], "flags": {"status": "partial"}}
        self.assertEqual(narrow_answer("names only", {"failed": failed, "good": players()}), "North\nSouth")

    def test_other_accounts_do_not_block_an_explicit_subject(self):
        other = players()
        other["items"] = [{"tag": "#Q0", "status": "ok", "data": {"name": "Other"}}]
        self.assertEqual(narrow_answer("names only for #P0 and #P2",
                                      {"other": other, "selected": players()}), "North\nSouth")
        with self.assertRaises(GroundingError):
            narrow_answer("names only for #P0 and #P2", {"other": other})

    def test_compact_failed_batches_and_refusals_are_not_observations(self):
        failed = {"source": "clash_api", "kind": "player", "items": {
            "columns": ["tag", "status"], "rows": [["#Q0", "not_found"]]}}
        refused = {**players(), "flags": {"status": "refused"}}
        self.assertEqual(narrow_answer("names only", {
            "failed": failed, "refused": refused, "good": players()}), "North\nSouth")
        with self.assertRaises(GroundingError):
            narrow_answer("names only", {"failed": failed, "refused": refused})

    def test_partial_successes_cannot_be_silently_dropped(self):
        partial = players()
        partial["flags"]["status"] = "partial"
        partial["items"][1]["status"] = "unavailable"
        with self.assertRaises(GroundingError):
            narrow_answer("names only", {"partial": partial, "other": query([{"name": "North"}])})

    def test_exact_duplicate_results_are_not_conflicts(self):
        for question, good in (("names only", players()), ("count only", query([{"count": 2}])),
                               ("Average stars for ranked defenses?", sample())):
            with self.subTest(question=question):
                self.assertEqual(narrow_answer(question, {"one": good, "two": deepcopy(good)}),
                                 narrow_answer(question, {"one": good}))

    def test_conflicting_successes_and_incomplete_requested_coverage_still_fail_closed(self):
        conflict = players()
        conflict["items"][0]["data"]["name"] = "Different"
        with self.assertRaises(GroundingError):
            narrow_answer("names only", {"one": players(), "two": conflict})
        partial = players()
        partial["items"][1]["status"] = "unavailable"
        with self.assertRaises(GroundingError):
            narrow_answer("names only", {"one": partial})


class AnswerRecoveryFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_strict_requests_recover_after_failure_in_the_original_request(self):
        for question, result, expected in (("names only", players(), "North\nSouth"),
                                           ("tags only", players(), "#P0\n#P2"),
                                           ("count only", query([{"count": 2}]), "2")):
            for reuse in (False, True):
                with self.subTest(question=question, reuse=reuse):
                    handler = AsyncMock(side_effect=[{"error": "The first read failed."}, result])
                    registry = {"read_clash": RegisteredAgentTool(AgentToolDefinition(
                        "read_clash", "Read selected records.", {"type": "object", "properties": {
                            "selection": {"type": "string"}}, "required": ["selection"]}), handler)}
                    def step(identity, selection):
                        return replace(_model_step(_plan([{"id": identity, "capability": "read_clash",
                            "arguments": {"selection": selection}, "reason": "Read selected records", "depends_on": []}])),
                            usage=AgentUsage(1000, 100))
                    session = _Session([step("read", "old"), step("read" if reuse else "corrected", "new"),
                                        AgentStep("Should not need synthesis.", (), AgentUsage(1000, 100))], [])
                    with patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value=registry):
                        answer = await AgentService(_Model(session)).answer(
                            question=question, local_context="", context=_context())
                    self.assertEqual(answer, expected)
                    self.assertEqual(handler.await_count, 2)
                    self.assertEqual(len(session.calls), 2)
