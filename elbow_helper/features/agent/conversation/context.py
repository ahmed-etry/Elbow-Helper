"""Whole-record context compilation with explicitly approximate input sizing."""

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import re
from typing import Mapping, Sequence

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..models import AgentRequestContext
from .state import (
    MAX_CHECKPOINT_BYTES, MAX_CHECKPOINT_REQUEST_IDS, ConversationCheckpoint,
    ConversationTurn, checkpoint_input_hash,
)
from ..prompts import SYSTEM_PROMPT, build_request_prompt
from ..identity import agent_identity, guild_identity


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
        cost = estimate_tokens(_history_text(turn)) + 1
        if cost > remaining:
            break
        selected.append(turn)
        remaining -= cost
    return tuple(reversed(selected))


def _history_text(turn):
    """Old snapshots' sliced excerpts cannot stand in for complete lookup records."""
    if turn.record is not None:
        # Nearby Discord context is already supplied for this request. Repeating
        # every old window multiplies context and can drown out corrections.
        return json.dumps({"member_id": turn.record.member_id,
                           "question": turn.record.question,
                           "answer": turn.record.delivered_answer,
                           "delivery_complete": turn.record.delivery_complete,
                           "delivery_unknown": turn.record.delivery_unknown,
                           "request_message_id": turn.record.request_message_id,
                           "report_ids": turn.record.report_ids}, ensure_ascii=False)
    if "lookup_excerpts" not in turn.text:
        return turn.text
    try:
        payload = json.loads(turn.text)
    except ValueError:
        return turn.text
    if not isinstance(payload, dict) or "lookup_excerpts" not in payload:
        return turn.text
    del payload["lookup_excerpts"]
    return json.dumps(payload, ensure_ascii=False)


def retained_knowledge_sources(turn):
    sources = set()
    for encoded in turn.record.evidence if turn.record is not None else ():
        try:
            record = json.loads(encoded)
            sources.update(value for value in record.get("knowledge_sources", ())
                           if isinstance(value, str) and len(value) == 64)
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


def _contextual_followup(text):
    return bool(re.search(
        r"\b(he|she|him|her|they|them|it|that|those|these|same|previous|recruits?|"
        r"continue|again|correction|wrong|failed|asked|everyone|their|his|hers)\b",
        text.casefold(),
    ) or re.search(r"^(?:actually\b|instead\b|use\b|include\b|exclude\b|no[,!. ])", text.casefold()))


def relevant_turns(question, turns):
    """Keep resolved subjects for follow-ups without importing unrelated cases."""
    text = question.casefold()
    matched = set()
    aliases = {}

    def rows(value):
        if isinstance(value, dict) and set(value) == {"columns", "rows"}:
            return [dict(zip(value["columns"], row)) for row in value["rows"]]
        return value if isinstance(value, list) else []

    for turn in turns:
        for encoded in turn.record.evidence if turn.record else ():
            try:
                record = json.loads(encoded)
                result = json.loads(record["result"])
                if record["tool"] == "find_discord_members":
                    entries = rows(result.get("members"))
                    keys = ("member_id", "display_name", "username")
                elif record["tool"] == "read_linked_accounts":
                    entries = rows(result.get("accounts"))
                    keys = ("member_id", "player_tag", "player_name")
                elif record["tool"] == "read_clash":
                    entries = [{**item.get("data", {}), "tag": item.get("tag")}
                               for item in rows(result.get("items")) if item.get("status") == "ok"]
                    keys = ("tag", "name")
                else:
                    continue
                for entry in entries:
                    values = {str(entry[key]).casefold() for key in keys if entry.get(key) is not None}
                    identities = values & {str(entry[key]).casefold() for key in ("member_id", "player_tag", "tag")
                                           if entry.get(key) is not None}
                    for identity in identities:
                        aliases.setdefault(identity, set()).update(values)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
    for identity, names in aliases.items():
        if any(len(name) >= 3 and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text) for name in names):
            matched.add(identity)
    if matched:
        def subjects(document):
            return {identity for identity, names in aliases.items() if any(
                len(name) >= 3 and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", document.casefold())
                for name in names)}

        selected, active = [], False
        for turn in turns:
            if turn.record is None:
                active = False
                continue
            record = turn.record
            # A question's explicit subject takes priority over incidental names
            # in a result set. Unnamed corrections inherit only the active task;
            # another subject or standalone turn ends that continuity.
            named = subjects(record.question) or subjects(record.delivered_answer + " " + " ".join(record.evidence))
            if named:
                active = bool(named & matched)
            elif active:
                active = _contextual_followup(record.question)
            if active:
                selected.append(turn)
        return tuple(selected[-8:])
    # Pronouns, cohort references and corrections depend on the ongoing task.
    # A new explicit question can use its own resolution tools instead of
    # loading a private case merely because it happened nearby.
    referential = _contextual_followup(text)
    standalone = re.search(r"^(?:@[^?]+?\s+)?(?:what(?:'s|’s| is)|who is|how does|tell me about)\b", text)
    return () if standalone and not referential else turns


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
    report_filter_fields: Mapping[str, Sequence[str]] | None = None,
    system_prompt: str = SYSTEM_PROMPT,
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
    manifest = []
    for report in context.state.reports.values():
        # Navigational handles do not expose a retained report's private counts
        # or summaries. Its reader authorizes and loads that evidence on demand.
        details = report.manifest()
        item = {"report_id": details.get("report_id"), "kind": details.get("kind"), "requires_read": True}
        item["filter_fields"] = list((report_filter_fields or {}).get(item.get("kind", ""), ()))
        manifest.append(item)
    candidates = context.state.authorized_history
    scoped = relevant_turns(question, candidates) if candidates is not None else None
    checkpoint = context.state.authorized_checkpoint if scoped == candidates else None
    candidates = scoped
    instructions = [asdict(instruction) for instruction in context.state.authorized_instructions]
    own_identity = agent_identity(getattr(context, "bot", None), context.guild)
    owner = getattr(context, "application_owner", None)
    if owner is not None:
        owner = guild_identity(owner, context.guild)
    for instruction in context.state.authorized_instructions:
        context.state.source_channels.add(instruction.source_channel_id)

    evidence = [(turn, model_evidence(encoded)) for turn in candidates or () if turn.record is not None
                for encoded in turn.record.evidence]
    carried = []

    def render(history: str, included: int, omitted: int, checkpoint_text: str = "") -> str:
        status = {"included_turns": included, "older_retained_turns_available": omitted,
                  "history_may_be_incomplete": True}
        dropped = len(evidence) - len(carried)
        if evidence:
            status.update(included_evidence_records=len(carried), omitted_evidence_records=dropped)
        if dropped:
            status["evidence_omitted_from_requests"] = list(dict.fromkeys(
                turn.record.request_message_id for turn, _ in evidence[:dropped]))
            status["evidence_recovery"] = "Whole lookup records were omitted. Re-read them before making claims."
        return build_request_prompt(
            question=question, local_context=local_context, guild_name=context.guild.name,
            asker_name=context.member.display_name, asker_id=context.member.id,
            asked_at=context.source_message.created_at,
            conversation_history=history, history_status=status, report_manifest=manifest,
            task_instructions=instructions, history_checkpoint=checkpoint_text,
            agent_identity=asdict(own_identity) if own_identity is not None else None,
            application_owner=asdict(owner) if owner is not None else None,
            carried_evidence=carried,
        )

    selected = []
    if candidates is None:
        # Compatibility for service callers supplying their own authorized text.
        prompt = render(conversation_history, 0, 0)
        omitted = 0
    else:
        prompt = render("", 0, len(candidates))
        base = estimate_tokens(system_prompt) + estimate_tokens(tool_json) + REQUEST_OVERHEAD_ESTIMATE
        remaining = max(0, target_tokens - base - estimate_tokens(prompt))
        selected = list(select_recent_turns(candidates, token_budget=remaining))
        omitted = len(candidates) - len(selected)
        checkpoint_text = ""
        if checkpoint is not None and checkpoint.covered_turn_count <= omitted:
            checkpoint_text = checkpoint.summary
        prompt = render(
            "\n".join(_history_text(turn) for turn in selected), len(selected), omitted,
            checkpoint_text,
        )
        if checkpoint_text and base + estimate_tokens(prompt) > target_tokens:
            checkpoint_text = ""
            prompt = render(
                "\n".join(_history_text(turn) for turn in selected), len(selected), omitted,
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
            context.state.knowledge_sources.update(retained_knowledge_sources(turn))
        remaining = max(0, target_tokens - base - estimate_tokens(prompt) - 128)
        for turn, encoded in reversed(evidence):
            item = {"request_message_id": turn.record.request_message_id, "record": encoded}
            cost = estimate_tokens(json.dumps(item, ensure_ascii=False, separators=(",", ":"))) + 1
            if cost > remaining:
                break
            carried.insert(0, item)
            remaining -= cost
        prompt = render("\n".join(_history_text(turn) for turn in selected),
                        len(selected), omitted, checkpoint_text)
        if len(carried) < len(evidence):
            partial = list(carried)
            carried[:] = [{"request_message_id": turn.record.request_message_id, "record": encoded}
                          for turn, encoded in evidence]
            complete = render("\n".join(_history_text(turn) for turn in selected),
                              len(selected), omitted, checkpoint_text)
            if base + estimate_tokens(complete) <= target_tokens:
                prompt = complete
            else:
                carried[:] = partial
        # The first pass reserves framing space. Reclaim it using the rendered
        # prompt so a complete record that fits at the boundary is not omitted.
        while len(carried) < len(evidence):
            turn, encoded = evidence[len(evidence) - len(carried) - 1]
            carried.insert(0, {"request_message_id": turn.record.request_message_id, "record": encoded})
            candidate = render("\n".join(_history_text(turn) for turn in selected),
                               len(selected), omitted, checkpoint_text)
            if base + estimate_tokens(candidate) > target_tokens:
                carried.pop(0)
                break
            prompt = candidate
        while carried and base + estimate_tokens(prompt) > target_tokens:
            carried.pop(0)
            prompt = render("\n".join(_history_text(turn) for turn in selected),
                            len(selected), omitted, checkpoint_text)
        for turn, _ in evidence[len(evidence) - len(carried):] if carried else ():
            context.state.source_channels.update(turn.source_channels)
            context.state.required_access.update(turn.required_access)
            context.state.knowledge_sources.update(retained_knowledge_sources(turn))
        context.state.history_status.update(included_evidence_records=len(carried),
                                           omitted_evidence_records=len(evidence) - len(carried))
    estimated = (estimate_tokens(system_prompt) + estimate_tokens(tool_json)
                 + estimate_tokens(prompt) + REQUEST_OVERHEAD_ESTIMATE)
    retained = {}
    for index, item in enumerate(carried):
        try:
            record = json.loads(item["record"])
            retained[str(index)] = json.loads(record["result"])
        except (KeyError, TypeError, ValueError):
            continue
    if retained:
        # Retained messages can ground references in a follow-up. They remain
        # nested so stale live data cannot replace a new top-level numeric read.
        context.state.grounding_evidence["retained"] = retained
    return CompiledContext(
        prompt=prompt, estimated_input_tokens=estimated,
        input_content_bytes=len((system_prompt + tool_json + prompt).encode("utf-8")),
        selected_request_ids=tuple(turn.record.request_message_id for turn in selected if turn.record),
        omitted_turns=omitted, exceeds_target=estimated > target_tokens,
    )
