from datetime import datetime, timezone
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from elbow_helper.features.agent.conversation.context import (
    build_history_checkpoint, compile_context, estimate_tokens,
)
from elbow_helper.features.agent.conversation.state import (
    ConversationRecord, ConversationTurn,
)
from elbow_helper.features.agent.models import AgentTurnState
from features.agent.report_helpers import make_event_report
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


    def test_purpose_context_uses_registered_public_command_help(self):
        context = _context()
        context.bot = SimpleNamespace(tree=SimpleNamespace(get_commands=lambda **_: []))
        registered = {"/plan": object(), "/grant coins": object()}
        with patch("elbow_helper.features.agent.conversation.context.discover_commands", return_value=registered):
            compiled = _compile(context)
        expected = {"source": "/help", "commands": [
            {"path": "/plan", "summary": "Get help planning an attack."}]}
        self.assertEqual(json.loads(compiled.answer_context)["bot_command_overview"],
                         {"commands": expected['commands']})
        self.assertIn(json.dumps(expected), compiled.prompt)

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

    def test_calibrated_estimate_keeps_structured_data_cost_above_prose(self):
        prose = "A member can link several accounts. " * 100
        structured = json.dumps([{"member_id": 123456789012345678, "tag": "#P0Y"}] * 100)
        self.assertTrue(len(prose) / 6 < estimate_tokens(prose) < len(prose) / 3)
        self.assertTrue(len(structured) / 4 < estimate_tokens(structured) < len(structured) / 2)

    def test_complete_lookup_records_survive_followups_beyond_old_excerpt_limits(self):
        records = tuple(json.dumps({"call_id": f"lookup-{index}", "tool": "synthetic_read",
                                    "result": "x" * 6000 + f"Evidence tail {index}"})
                        for index in range(7))
        turn = _turn(1, channel=200)
        turn = replace(turn, record=replace(turn.record, evidence=records),
                       required_access=frozenset({"lead"}))
        context = _context((turn,))
        context.guild.name = "Synthetic server"
        compiled = _compile(context)
        carried = json.loads(compiled.prompt.split("<carried_evidence>\n")[1].split("\n</carried_evidence>")[0])
        self.assertEqual([item["record"] for item in carried], list(records))
        self.assertEqual(context.state.source_channels, {200})
        self.assertEqual(context.state.required_access, {"lead"})
        self.assertFalse(compiled.exceeds_target)

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

    def test_legacy_excerpts_are_replaced_by_full_retained_evidence(self):
        turn = _turn(1)
        turn = replace(turn, text=json.dumps({"answer": "Synthetic answer", "lookup_excerpts": ["cut record"]}),
                       record=replace(turn.record, evidence=("Complete synthetic evidence",)))
        context = _context((turn,))
        compiled = _compile(context)
        self.assertNotIn("lookup_excerpts", compiled.prompt)
        self.assertNotIn("cut record", compiled.prompt)
        self.assertIn("Complete synthetic evidence", compiled.prompt)

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

    def test_actual_system_instructions_reduce_evidence_allowance(self):
        turn = _turn(1)
        turn = replace(turn, record=replace(turn.record, evidence=("x" * 10_000,)))
        context = _context((turn,))
        baseline = _compile(context, target_tokens=20_000)
        larger = _compile(_context((turn,)), target_tokens=20_000, system_prompt="x" * 90_000)
        self.assertGreater(larger.estimated_input_tokens - baseline.estimated_input_tokens, 0)
        self.assertEqual(larger.prompt.split("<carried_evidence>\n")[1].split("\n</carried_evidence>")[0], "[]")

    def test_available_report_names_its_supported_filters(self):
        context = _context()
        context.state.reports["synthetic"] = SimpleNamespace(
            manifest=lambda: {"report_id": "synthetic", "kind": "synthetic"},
        )
        compiled = _compile(context, report_filter_fields={"synthetic": ("offset", "limit")})
        manifest_text = compiled.prompt.split("<available_reports>\n", 1)[1].split(
            "\n</available_reports>", 1,
        )[0]
        self.assertEqual(json.loads(manifest_text)[0]["filter_fields"], ["offset", "limit"])

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
        result = _compile(context, target_tokens=base + 600)
        history = result.prompt.split("<conversation_history>\n", 1)[1].split("\n</conversation_history>", 1)[0]
        records = [json.loads(line) for line in history.splitlines()]
        self.assertEqual([record["question"] for record in records], ["3", "4"])
        self.assertEqual(result.omitted_turns, 2)
        self.assertEqual(context.state.authorized_history, turns)
        self.assertEqual(context.state.source_channels, {3, 4})
        self.assertLessEqual(result.estimated_input_tokens, base + 600)

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
        context.state.reports["report"] = make_event_report("report", "now")
        result = _compile(context)
        self.assertLess(result.prompt.index("</conversation_history>"), result.prompt.index("<available_reports>"))
        self.assertIn("\"kind\": \"event_schedule\"", result.prompt)

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
