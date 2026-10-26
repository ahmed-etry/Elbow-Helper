"""Recover presentation mistakes without accepting different scope signals."""

import json
from elbow_helper.features.agent.wording import AGENT_ANSWER_UNFINISHED
import unittest

from elbow_helper.features.agent.knowledge.answers import scoped_answer
from elbow_helper.features.agent.wording import FAILURE_MESSAGE
from features.agent.engine.test_agent_plan_flow import _context


class ScopedAnswerRecoveryTests(unittest.TestCase):
    def test_answer_only_envelopes_never_escape_regardless_of_fence_or_nesting(self):
        text = "BE4's posted bar is **TH15–TH18**.\n- North qualifies."
        plain = json.dumps({"answer": text})
        for value in (plain, "```json\n" + plain + "\n```",
                      json.dumps({"answer": plain}),
                      json.dumps({"needs_private_evidence": False, "answer": plain})):
            with self.subTest(value=value):
                context = _context()
                self.assertEqual(scoped_answer(context, value), text)
                self.assertFalse(context.state.scope_audit.offered)

    def test_answer_first_invalid_envelopes_fail_closed_without_an_offer(self):
        for value in ('{"answer":', '{"answer":"Facts", "needs_private_evidence":',
                      '{"extra":1,"answer":',
                      '{"answer":42}', '{"answer":"Facts", "extra":1}',
                      '{"answer":"Facts", "needs_restricted_knowledge":true}',
                      json.dumps({"needs_private_evidence": True, "answer": '{"answer":'})):
            for excluded in (False, True):
                with self.subTest(value=value, excluded=excluded):
                    context = _context()
                    context.state.scope_audit.private_candidates.add("restricted")
                    if excluded:
                        context.state.scope_audit.excluded["restricted"] = "per_person"
                    self.assertEqual(scoped_answer(context, value),
                                     AGENT_ANSWER_UNFINISHED)
                    self.assertFalse(context.state.scope_audit.offered)

    def test_unrelated_json_and_plain_text_remain_intact(self):
        for value in ('{"members":["North"]}', "North meets BE4's bar."):
            self.assertEqual(scoped_answer(_context(), value), value)

    def test_literal_linebreaks_preserve_supported_findings(self):
        context = _context()
        answer = '{"needs_private_evidence":true,"answer":"Available findings:\n- Three completed attacks.\nMissing: September defenses."}'
        result = scoped_answer(context, answer)
        self.assertIn("Three completed attacks", result)
        self.assertIn("September defenses", result)
        self.assertNotEqual(result, FAILURE_MESSAGE)
        self.assertFalse(context.state.scope_audit.offered)

    def test_relaxed_strings_do_not_accept_extra_keys_or_wrong_signal_types(self):
        for value in ('{"needs_private_evidence":true,"answer":"Facts\nremain","scope":"all"}',
                      '{"needs_private_evidence":"true","answer":"Facts\nremain"}',
                      '{"needs_private_evidence":true,"answer":{}}'):
            self.assertEqual(scoped_answer(_context(), value), AGENT_ANSWER_UNFINISHED)

    def test_multiline_findings_survive_excluded_evidence_with_either_boolean_signal(self):
        for flag in ("true", "false"):
            with self.subTest(flag=flag):
                context = _context()
                context.state.scope_audit.excluded["restricted"] = "per_person"
                answer = ('{"needs_private_evidence":' + flag
                          + ',"answer":"Three attacks completed.\nSeptember defenses unavailable here."}')
                self.assertEqual(scoped_answer(context, answer),
                                 "Three attacks completed.\nSeptember defenses unavailable here.")
                self.assertFalse(context.state.scope_audit.offered)

    def test_excluded_evidence_keeps_scope_empty_distinct_from_failure(self):
        context = _context()
        context.state.scope_audit.excluded["restricted"] = "per_person"
        for answer in ("", FAILURE_MESSAGE, '{"needs_private_evidence":',
                       '{"needs_private_evidence":true,"answer":"' + FAILURE_MESSAGE + '"}',
                       '{"needs_private_evidence":true,"answer":"Facts\nremain","scope":"all"}'):
            with self.subTest(answer=answer):
                self.assertEqual(scoped_answer(context, answer), AGENT_ANSWER_UNFINISHED)
