"""Whole-record context compilation with explicitly approximate input sizing."""

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from typing import Sequence

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..models import AgentRequestContext
from .state import (
    MAX_CHECKPOINT_BYTES, MAX_CHECKPOINT_REQUEST_IDS, ConversationCheckpoint,
    ConversationTurn, checkpoint_input_hash,
)
from ..prompts import SYSTEM_PROMPT, build_request_prompt


# A soft working target, not a model limit or an estimate of billed tokens.
INPUT_TARGET_TOKENS = 64_000
REQUEST_OVERHEAD_ESTIMATE = 512
CHECKPOINT_MIN_OMITTED_TURNS = 8
CHECKPOINT_MIN_GROWTH = 8
CHECKPOINT_HEAD_ENTRIES = 16
CHECKPOINT_TAIL_ENTRIES = 48


def estimate_tokens(text: str) -> int:
    """Conservative heuristic, pending calibration against deployed-model usage.

    ASCII letters/spaces use two characters per token; punctuation, digits and
    UTF-8 bytes use one per token. This intentionally differs from a tokenizer.
    It can over/under-estimate and must never be used as a billing total.
    """
    weighted = sum(1 if char.isascii() and (char.isalpha() or char.isspace())
                   else 2 * len(char.encode("utf-8")) for char in text)
    return (weighted + 1) // 2


@dataclass(frozen=True, slots=True)
class CompiledContext:
    prompt: str
    estimated_input_tokens: int
    input_content_bytes: int
    selected_request_ids: tuple[int, ...]
    omitted_turns: int
    exceeds_target: bool


def select_recent_turns(turns: Sequence[ConversationTurn], *, token_budget: int) -> tuple[ConversationTurn, ...]:
    selected = []
    remaining = token_budget
    for turn in reversed(turns):
        cost = estimate_tokens(turn.text) + 1
        if cost > remaining:
            break
        selected.append(turn)
        remaining -= cost
    return tuple(reversed(selected))


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


def compile_context(
    *, question: str, local_context: str, context: AgentRequestContext,
    tools: Sequence[AgentToolDefinition], conversation_history: str = "",
    target_tokens: int = INPUT_TARGET_TOKENS,
) -> CompiledContext:
    """Preserve mandatory input and choose whole authorized recent records.

    Oversized mandatory input is reported, never silently truncated. Retention
    and authorization are owned by the caller; compilation does not delete data.
    """
    if target_tokens <= 0:
        raise ValueError("Context target must be positive")
    definitions = [{"type": "function", "function": {
        "name": tool.name, "description": tool.description, "parameters": tool.parameters,
    }} for tool in tools]
    tool_json = json.dumps(definitions, ensure_ascii=False, sort_keys=True)
    manifest = [report.manifest() for report in context.state.reports.values()]
    candidates = context.state.authorized_history
    checkpoint = context.state.authorized_checkpoint
    instructions = [asdict(instruction) for instruction in context.state.authorized_instructions]
    for instruction in context.state.authorized_instructions:
        context.state.source_channels.add(instruction.source_channel_id)

    def render(history: str, included: int, omitted: int, checkpoint_text: str = "") -> str:
        status = {"included_turns": included, "older_retained_turns_available": omitted,
                  "history_may_be_incomplete": True}
        return build_request_prompt(
            question=question, local_context=local_context, guild_name=context.guild.name,
            asker_name=context.member.display_name, asked_at=context.source_message.created_at,
            conversation_history=history, history_status=status, report_manifest=manifest,
            task_instructions=instructions, history_checkpoint=checkpoint_text,
        )

    selected = []
    if candidates is None:
        # Compatibility for service callers supplying their own authorized text.
        prompt = render(conversation_history, 0, 0)
        omitted = 0
    else:
        prompt = render("", 0, len(candidates))
        base = estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(tool_json) + REQUEST_OVERHEAD_ESTIMATE
        remaining = max(0, target_tokens - base - estimate_tokens(prompt))
        selected = list(select_recent_turns(candidates, token_budget=remaining))
        omitted = len(candidates) - len(selected)
        checkpoint_text = ""
        if checkpoint is not None and checkpoint.covered_turn_count <= omitted:
            checkpoint_text = checkpoint.summary
        prompt = render(
            "\n".join(turn.text for turn in selected), len(selected), omitted,
            checkpoint_text,
        )
        if checkpoint_text and base + estimate_tokens(prompt) > target_tokens:
            checkpoint_text = ""
            prompt = render(
                "\n".join(turn.text for turn in selected), len(selected), omitted,
                checkpoint_text,
            )
        elif checkpoint_text:
            context.state.source_channels.update(checkpoint.source_channels)
            context.state.required_access.update(checkpoint.required_access)
        context.state.history_status = {"included_turns": len(selected),
            "older_retained_turns_available": omitted, "history_may_be_incomplete": True}
        for turn in selected:
            context.state.source_channels.update(turn.source_channels)
            context.state.required_access.update(turn.required_access)
    estimated = (estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(tool_json)
                 + estimate_tokens(prompt) + REQUEST_OVERHEAD_ESTIMATE)
    return CompiledContext(
        prompt=prompt, estimated_input_tokens=estimated,
        input_content_bytes=len((SYSTEM_PROMPT + tool_json + prompt).encode("utf-8")),
        selected_request_ids=tuple(turn.record.request_message_id for turn in selected if turn.record),
        omitted_turns=omitted, exceeds_target=estimated > target_tokens,
    )
