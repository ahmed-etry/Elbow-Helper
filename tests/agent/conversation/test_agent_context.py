from datetime import datetime, timezone
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest

from elbow_helper.agent.conversation.context import build_history_checkpoint, compile_context
from elbow_helper.agent.conversation.state import (
    ConversationRecord, ConversationTurn,
)
from elbow_helper.agent.models import AgentTurnState
from elbow_helper.infrastructure.ai import AgentToolDefinition


def _context(turns=()):
    member = SimpleNamespace(display_name="Tester", id=7, roles=[])
    return SimpleNamespace(
        guild=SimpleNamespace(name="Brown Elbow", get_member=lambda key: member if key == member.id else None),
        member=member,
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
        delivered_answer=f"Answer {index}" + "x" * payload_characters, local_context="", evidence=(),
        report_ids=(f"report-{index}",), reply_ids=(1000 + index,),
        delivery_complete=True,
    )
    return ConversationTurn(
        json.dumps({
            "question": record.question,
            "answer": record.delivered_answer,
        }),
        frozenset({channel}), record=record,
    )


def _compile(context, **changes):
    values = dict(question="Continue", local_context="Nearby discussion", context=context, tools=())
    values.update(changes)
    return compile_context(**values)


class AgentContextTests(unittest.TestCase):
    def test_current_room_and_thread_identity_reach_planner_and_writer_intact(self):
        context = _context()
        category = SimpleNamespace(id=12, name='Cat clan')
        parent = SimpleNamespace(id=13, name='bec-war-discussion', category=category)
        context.source_message.channel = SimpleNamespace(id=14, name='Today\'s war',
            parent=parent, category=None, topic='Use the clan plan')
        compiled = _compile(context)
        expected = {'channel_id': 14, 'name': "Today's war", 'topic': 'Use the clan plan',
            'parent': {'channel_id': 13, 'name': 'bec-war-discussion'},
            'category': {'channel_id': 12, 'name': 'Cat clan'}}
        planner = compiled.prompt.split('<current_channel>\n', 1)[1].split('\n</current_channel>', 1)[0]
        self.assertEqual(json.loads(planner), expected)
        self.assertEqual(json.loads(compiled.answer_context)['current_channel'], expected)
        self.assertEqual(context.state.source_channels, set())


    def test_referential_and_named_followups_keep_all_recent_authorized_turns(self):
        turns = tuple(replace(turn, record=replace(turn.record, question=question))
                      for turn, question in zip((_turn(1), _turn(2), _turn(3)), (
                          "List Alpha and Beta", "Use last season's data", "Check Alpha's accounts",
                      )))
        for question in ("what is the league of the second one?",
                         "what's the TH of the last guy you listed?",
                         "tell me about the other clan", "What is Alpha's league?",
                         "What's another member's league?"):
            with self.subTest(question=question):
                compiled = _compile(_context(turns), question=question)
                self.assertEqual(compiled.selected_request_ids, (1, 2, 3))
                self.assertIn("Use last season's data", compiled.prompt)


    def test_evidence_budget_drops_oldest_whole_records_and_marks_recovery(self):
        records = tuple(json.dumps({"call_id": f"lookup-{index}", "result": "x" * 800,
                                    "marker": f"Synthetic result {index}"}) for index in range(5))
        turn = _turn(1)
        turn = replace(turn, record=replace(turn.record, evidence=records))
        context = _context((turn,))
        baseline = _compile(_context((replace(turn, record=replace(turn.record, evidence=())),)))
        compiled = _compile(context, target_tokens=baseline.estimated_input_tokens + 700)
        carried = json.loads(compiled.prompt.split("<carried_evidence>\n")[1].split("\n</carried_evidence>")[0])
        self.assertGreater(len(carried), 0)
        self.assertLess(len(carried), len(records))
        self.assertEqual([item["record"] for item in carried], list(records[-len(carried):]))
        status = json.loads(compiled.prompt.split("<history_status>\n")[1].split("\n</history_status>")[0])
        self.assertEqual(status["omitted_evidence_records"], len(records) - len(carried))
        self.assertEqual(status["evidence_omitted_from_requests"], [1])
        self.assertIn("Re-read", status["evidence_recovery"])
        self.assertEqual(json.loads(compiled.answer_context)["history_status"], status)
        self.assertFalse(compiled.exceeds_target)
        self.assertEqual(context.state.authorized_history[0].record.evidence, records)


    def test_complete_evidence_is_kept_when_it_exactly_fits_the_context_target(self):
        for records in (("Synthetic evidence " + "x" * 1000,), ("Synthetic", "Evidence")):
            with self.subTest(records=len(records)):
                turn = _turn(1)
                turn = replace(turn, record=replace(turn.record, evidence=records))
                roomy = _compile(_context((turn,)))
                exact = _compile(_context((turn,)), target_tokens=roomy.estimated_input_tokens)
                carried = json.loads(exact.prompt.split("<carried_evidence>\n")[1].split("\n</carried_evidence>")[0])
                self.assertEqual([item["record"] for item in carried], list(records))
                self.assertFalse(exact.exceeds_target)


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
        result = _compile(context, target_tokens=base + 6_000)
        self.assertGreaterEqual(result.omitted_turns, 8)
        self.assertIn(checkpoint.summary, result.prompt)
        self.assertEqual(context.state.source_channels, {100})

        roomy = _context(turns)
        roomy.state.authorized_checkpoint = checkpoint
        rendered = _compile(roomy, target_tokens=100_000)
        self.assertEqual(rendered.omitted_turns, 0)
        self.assertNotIn(checkpoint.summary, rendered.prompt)


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


    def test_tools_and_local_context_reduce_history_allowance(self):
        turns = tuple(ConversationTurn("x" * 1000, frozenset()) for _ in range(20))
        target_tokens = _compile(_context()).estimated_input_tokens + 2_000
        baseline = _compile(_context(turns), target_tokens=target_tokens)
        tool = AgentToolDefinition("lookup", "z" * 6000, {"type": "object", "properties": {}})
        larger = _compile(_context(turns), tools=(tool,), local_context="y" * 4000, target_tokens=target_tokens)
        self.assertGreater(baseline.omitted_turns, 0)
        self.assertLess(baseline.omitted_turns, len(turns))
        self.assertGreater(larger.omitted_turns, baseline.omitted_turns)

    def test_oversized_mandatory_request_is_retained_and_flagged(self):
        question = "Keep this exact constraint " + "x" * 10_000
        result = _compile(_context(), question=question, target_tokens=100)
        self.assertIn(question, result.prompt)
        self.assertTrue(result.exceeds_target)


    def test_oversized_recent_record_is_retrievable_not_sliced(self):
        turn = ConversationTurn('{"text":"' + "x" * 10_000 + '"}', frozenset({200}))
        context = _context((turn,))
        result = _compile(context, target_tokens=3000)
        self.assertEqual(result.omitted_turns, 1)
        self.assertNotIn('"text"', result.prompt)
        self.assertIs(context.state.authorized_history[0], turn)
        self.assertEqual(context.state.source_channels, set())
