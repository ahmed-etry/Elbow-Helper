from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest

from elbow_helper.agent.conversation.context import build_history_checkpoint
from elbow_helper.agent.conversation.state import (
    ConversationRecord, ConversationTurn,
)
from elbow_helper.agent.engine.models import AgentTurnState


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


class AgentContextTests(unittest.TestCase):

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
