"""Conversation checkpoints and retained lookup records."""

from datetime import datetime
import json
from typing import Sequence

from .state import (
    MAX_CHECKPOINT_BYTES, MAX_CHECKPOINT_REQUEST_IDS, ConversationCheckpoint,
    ConversationTurn, checkpoint_input_hash,
)


CHECKPOINT_MIN_OMITTED_TURNS = 8
CHECKPOINT_MIN_GROWTH = 8
CHECKPOINT_HEAD_ENTRIES = 16
CHECKPOINT_TAIL_ENTRIES = 48


def estimate_tokens(text: str) -> int:
    """Approximate DeepSeek input sizing fitted to archived round usage.

    ASCII prose uses five characters per token, syntax/digits five per three
    tokens, and non-ASCII two UTF-8 bytes per token. This is a working budget
    heuristic, never a billing total or a hard context-window bound.
    """
    weighted = sum((2 if char.isalpha() or char.isspace() else 6) if char.isascii()
                   else 5 * len(char.encode("utf-8")) for char in text)
    return (weighted + 9) // 10


def retained_knowledge_sources(turn):
    sources = set()
    for encoded in turn.record.evidence if turn.record is not None else ():
        try:
            record = json.loads(encoded)
            sources.update(value for value in record.get("knowledge_sources", ())
                           if isinstance(value, str) and 12 <= len(value) <= 64)
        except (TypeError, ValueError, AttributeError):
            continue
    return sources


def model_evidence(encoded):
    """Replay only the exact delivered model view, retaining raw records internally."""
    try:
        record = json.loads(encoded)
        if "model_result" in record:
            record["result"] = record.pop("model_result")
        elif record.get("result_complete") is False:
            record["result"] = json.dumps({"flags": {"status": "partial"},
                                           "cause": "retained_model_view_unavailable"})
        else:
            return encoded
        return json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError, AttributeError):
        return encoded


def build_history_checkpoint(
    turns: Sequence[ConversationTurn], *, covered_turn_count: int,
    created_at: datetime,
) -> ConversationCheckpoint:
    """Build a bounded deterministic navigation digest over retained turns."""

    if (
        type(covered_turn_count) is not int
        or not CHECKPOINT_MIN_OMITTED_TURNS <= covered_turn_count <= len(turns)
        or created_at.tzinfo is None
        or created_at.utcoffset() is None
    ):
        raise ValueError("Invalid checkpoint coverage")
    covered = tuple(turns[:covered_turn_count])
    records = tuple(turn.record for turn in covered if turn.record is not None)
    request_ids = tuple(record.request_message_id for record in records)
    if len(request_ids) > MAX_CHECKPOINT_REQUEST_IDS:
        request_ids = (*request_ids[:CHECKPOINT_HEAD_ENTRIES],
                       *request_ids[-(MAX_CHECKPOINT_REQUEST_IDS - CHECKPOINT_HEAD_ENTRIES):])
    selected_records = records
    if len(records) > CHECKPOINT_HEAD_ENTRIES + CHECKPOINT_TAIL_ENTRIES:
        selected_records = (
            *records[:CHECKPOINT_HEAD_ENTRIES],
            *records[-CHECKPOINT_TAIL_ENTRIES:],
        )
    question_limit, answer_limit = 300, 500
    while True:
        entries = [{
            "request_message_id": record.request_message_id,
            "created_at": record.created_at,
            "question": _checkpoint_excerpt(record.question, question_limit),
            "visible_answer": _checkpoint_excerpt(
                record.delivered_answer, answer_limit,
            ),
            "delivery_complete": record.delivery_complete,
            "delivery_unknown": record.delivery_unknown,
            "report_ids": list(record.report_ids),
        } for record in selected_records]
        summary = json.dumps({
            "format": "deterministic_record_digest_v1",
            "covered_turn_count": covered_turn_count,
            "covered_record_count": len(records),
            "omitted_digest_entries": len(records) - len(selected_records),
            "entries": entries,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(summary.encode("utf-8")) <= MAX_CHECKPOINT_BYTES:
            break
        if question_limit <= 40 and answer_limit <= 40:
            raise ValueError("Conversation checkpoint metadata exceeds its storage budget")
        question_limit = max(40, question_limit // 2)
        answer_limit = max(40, answer_limit // 2)
    return ConversationCheckpoint(
        covered_turn_count=covered_turn_count,
        covered_request_ids=request_ids,
        summary=summary,
        source_channels=frozenset(
            channel_id for turn in covered for channel_id in turn.source_channels
        ),
        required_access=frozenset(
            requirement for turn in covered for requirement in turn.required_access
        ),
        input_hash=checkpoint_input_hash(covered),
        created_at=created_at.isoformat(),
    )


def _checkpoint_excerpt(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit - 1] + "…"
