from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest

from elbow_helper.features.agent.conversation.context import (
    build_history_checkpoint, compile_context, estimate_tokens,
)
from elbow_helper.features.agent.conversation.state import (
    ConversationRecord, ConversationTurn,
)
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.reports.roles import RoleAccountReport
from elbow_helper.infrastructure.ai import AgentToolDefinition


def _context(turns=()):
    return SimpleNamespace(
        guild=SimpleNamespace(name="Brown Elbow"), member=SimpleNamespace(display_name="Tester"),
        source_message=SimpleNamespace(created_at=datetime(2026, 9, 17, tzinfo=timezone.utc)),
        state=AgentTurnState(authorized_history=tuple(turns)),
    )


def _turn(
    index: int, *, channel: int = 100, payload_characters: int = 0,
) -> ConversationTurn:
    record = ConversationRecord(
        request_message_id=index, member_id=42,
        created_at=f"2026-09-17T12:{index:02d}:00+00:00",
        question=f"Question {index}", generated_answer=f"Answer {index}",
        delivered_answer=f"Answer {index}", local_context="", evidence=(),
        report_ids=(f"report-{index}",), reply_ids=(1000 + index,),
        delivery_complete=True,
    )
    return ConversationTurn(
        json.dumps({
            "question": record.question,
            "answer": record.delivered_answer + "x" * payload_characters,
        }),
        frozenset({channel}), record=record,
    )


def _compile(context, **changes):
    values = dict(question="Continue", local_context="Nearby discussion", context=context, tools=())
    values.update(changes)
    return compile_context(**values)


class AgentContextTests(unittest.TestCase):
    def test_checkpoint_is_deterministic_bounded_and_used_only_for_omitted_prefix(self):
        turns = tuple(
            _turn(index, payload_characters=10_000)
            for index in range(1, 13)
        )
        checkpoint = build_history_checkpoint(
            turns, covered_turn_count=8,
            created_at=datetime(2026, 9, 17, 13, tzinfo=timezone.utc),
        )
        self.assertEqual(
            checkpoint,
            build_history_checkpoint(
                turns, covered_turn_count=8,
                created_at=datetime(2026, 9, 17, 13, tzinfo=timezone.utc),
            ),
        )
        context = _context(turns)
        context.state.authorized_checkpoint = checkpoint
        base = _compile(_context()).estimated_input_tokens
        result = _compile(context, target_tokens=base + 8_000)
        self.assertGreaterEqual(result.omitted_turns, 8)
        self.assertIn(checkpoint.summary, result.prompt)
        self.assertEqual(context.state.source_channels, {100})

        roomy = _context(turns)
        roomy.state.authorized_checkpoint = checkpoint
        rendered = _compile(roomy, target_tokens=100_000)
        self.assertEqual(rendered.omitted_turns, 0)
        self.assertNotIn(checkpoint.summary, rendered.prompt)

    def test_checkpoint_never_displaces_recent_verbatim_turns(self):
        turns = tuple(_turn(index) for index in range(1, 13))
        checkpoint = build_history_checkpoint(
            turns, covered_turn_count=8,
            created_at=datetime(2026, 9, 17, 13, tzinfo=timezone.utc),
        )
        context = _context(turns)
        context.state.authorized_checkpoint = checkpoint
        baseline = _compile(_context(turns), target_tokens=2200)
        result = _compile(context, target_tokens=2200)
        self.assertEqual(result.selected_request_ids, baseline.selected_request_ids)
        if checkpoint.summary not in result.prompt:
            self.assertEqual(result.estimated_input_tokens, baseline.estimated_input_tokens)

    def test_checkpoint_shrinks_escaped_excerpts_to_storage_budget(self):
        turns = []
        for index in range(1, 65):
            record = ConversationRecord(
                request_message_id=index, member_id=42,
                created_at=f"2026-09-17T12:{index:02d}:00+00:00",
                question='\\' * 5_000, generated_answer="answer",
                delivered_answer='\\' * 5_000, local_context="", evidence=(),
                report_ids=(f"report-{index}",), reply_ids=(1000 + index,),
                delivery_complete=True,
            )
            turns.append(ConversationTurn(
                f"turn {index}", frozenset({100}), record=record,
            ))
        checkpoint = build_history_checkpoint(
            turns, covered_turn_count=len(turns),
            created_at=datetime(2026, 9, 17, 13, tzinfo=timezone.utc),
        )
        self.assertLessEqual(
            len(checkpoint.summary.encode("utf-8")), 64 * 1024,
        )

    def test_whole_records_selected_without_deleting_candidates(self):
        turns = tuple(ConversationTurn(json.dumps({"question": str(index), "answer": "x" * 1000}), frozenset({index}))
                      for index in range(1, 5))
        context = _context(turns)
        base = _compile(_context()).estimated_input_tokens
        result = _compile(context, target_tokens=base + 1200)
        history = result.prompt.split("<conversation_history>\n", 1)[1].split("\n</conversation_history>", 1)[0]
        records = [json.loads(line) for line in history.splitlines()]
        self.assertEqual([record["question"] for record in records], ["3", "4"])
        self.assertEqual(result.omitted_turns, 2)
        self.assertEqual(context.state.authorized_history, turns)
        self.assertEqual(context.state.source_channels, {3, 4})
        self.assertLessEqual(result.estimated_input_tokens, base + 1200)

    def test_tools_and_local_context_reduce_history_allowance(self):
        turns = tuple(ConversationTurn("x" * 1000, frozenset()) for _ in range(20))
        baseline = _compile(_context(turns), target_tokens=10_000)
        tool = AgentToolDefinition("lookup", "z" * 6000, {"type": "object", "properties": {}})
        larger = _compile(_context(turns), tools=(tool,), local_context="y" * 4000, target_tokens=10_000)
        self.assertGreater(larger.omitted_turns, baseline.omitted_turns)

    def test_oversized_mandatory_request_is_retained_and_flagged(self):
        question = "Keep this exact constraint " + "x" * 10_000
        result = _compile(_context(), question=question, target_tokens=100)
        self.assertIn(question, result.prompt)
        self.assertTrue(result.exceeds_target)

    def test_short_turns_do_not_have_a_turn_count_cutoff(self):
        context = _context(tuple(ConversationTurn("small", frozenset()) for _ in range(100)))
        result = _compile(context)
        self.assertEqual(result.omitted_turns, 0)
        self.assertEqual(context.state.history_status["included_turns"], 100)

    def test_oversized_recent_record_is_retrievable_not_sliced(self):
        turn = ConversationTurn('{"text":"' + "x" * 10_000 + '"}', frozenset({200}))
        context = _context((turn,))
        result = _compile(context, target_tokens=3000)
        self.assertEqual(result.omitted_turns, 1)
        self.assertNotIn('"text"', result.prompt)
        self.assertIs(context.state.authorized_history[0], turn)
        self.assertEqual(context.state.source_channels, set())

    def test_report_manifest_follows_history_not_inside_stable_prefix(self):
        context = _context((ConversationTurn("stable", frozenset()),))
        context.state.reports["report"] = RoleAccountReport("report", "now", (), ())
        result = _compile(context)
        self.assertLess(result.prompt.index("</conversation_history>"), result.prompt.index("<available_reports>"))
        self.assertIn('"kind": "role_accounts"', result.prompt)

    def test_changed_metadata_does_not_change_history_prefix(self):
        context = _context((ConversationTurn("stable", frozenset()),))
        first = _compile(context)
        context.member.display_name = "Another tester"
        context.source_message.created_at = datetime(2026, 9, 18, tzinfo=timezone.utc)
        second = _compile(context)
        self.assertEqual(first.prompt.split("</conversation_history>")[0], second.prompt.split("</conversation_history>")[0])

    def test_estimator_counts_unicode_and_json_syntax_without_claiming_exactness(self):
        self.assertGreater(estimate_tokens("界" * 100), estimate_tokens("x" * 100))
        self.assertGreater(estimate_tokens("123{}" * 100), estimate_tokens("abcde" * 100))
        self.assertEqual(estimate_tokens(""), 0)
