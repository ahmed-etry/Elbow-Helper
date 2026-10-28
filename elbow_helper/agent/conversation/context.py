"""Whole-record context compilation with explicitly approximate input sizing."""

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from typing import Mapping, Sequence

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..models import AgentRequestContext
from .state import (
    MAX_CHECKPOINT_BYTES, MAX_CHECKPOINT_REQUEST_IDS, ConversationCheckpoint,
    ConversationTurn, checkpoint_input_hash,
)
from ..prompts import SYSTEM_PROMPT, build_request_prompt
from ..identity import agent_identity, guild_identity
from .reply_context import pinned_turns
from .findings import FINDINGS_INSTRUCTION, retained_finding
from elbow_helper.features.help.catalog import HELP_ENTRIES
from elbow_helper.features.help.discovery import discover_commands


# A soft working target, not a model limit or an estimate of billed tokens.
INPUT_TARGET_TOKENS = 32_000
SOCIAL_INPUT_TARGET_TOKENS = 16_000
REQUEST_OVERHEAD_ESTIMATE = 512
CHECKPOINT_MIN_OMITTED_TURNS = 8
CHECKPOINT_MIN_GROWTH = 8
CHECKPOINT_HEAD_ENTRIES = 16
CHECKPOINT_TAIL_ENTRIES = 48


def estimate_tokens(text: str) -> int:
    """Approximate DeepSeek input sizing fitted to archived round usage.

    ASCII prose uses five characters per token, syntax/digits five per three
    tokens, and non-ASCII two UTF-8 bytes per token. See the offline calibration
    in tests/agent/conversation/calibrate_tokens.py. This is a working
    budget heuristic, never a billing total or a hard context-window bound.
    """
    weighted = sum((2 if char.isalpha() or char.isspace() else 6) if char.isascii()
                   else 5 * len(char.encode("utf-8")) for char in text)
    return (weighted + 9) // 10


def bot_command_overview(context):
    """Describe public tasks from canonical help, without exposing restricted commands."""
    bot = getattr(context, "bot", None)
    registered = (discover_commands(bot)
                  if callable(getattr(getattr(bot, "tree", None), "get_commands", None)) else None)
    return {"source": "/help", "commands": [
        {"path": entry.path, "summary": entry.summary}
        for entry in HELP_ENTRIES if entry.visible_to is None
        and (registered is None or entry.path in registered)
    ]}


def requester_command_overview(context):
    """Use /help's role visibility for registered command summaries.

    Detailed command help still checks destination scope in its own reader.
    Keep the public self-description separate from the requester's catalogue.
    """
    from ..access import ACCESS_ROLE_SETS

    bot = getattr(context, 'bot', None)
    registered = (discover_commands(bot)
                  if callable(getattr(getattr(bot, 'tree', None), 'get_commands', None)) else None)
    member = context.guild.get_member(context.member.id)
    held = {role.id for role in member.roles} if member else set()
    entries = [entry for entry in HELP_ENTRIES
               if (entry.visible_to is None or held & entry.visible_to)
               and (registered is None or entry.path in registered)]
    # These summaries depend on requester roles, not restricted source records.
    # Recheck those roles before sending the compiled context to any provider.
    context.state.help_requester_access.update(level for level, roles in ACCESS_ROLE_SETS.items()
        if any(entry.visible_to == roles for entry in entries))
    return {'source': '/help', 'visibility': 'requester_roles',
            'commands': [{'path': entry.path, 'summary': entry.summary} for entry in entries]}


def bot_read_overview(context):
    """Describe implemented family reads independently of slash-command help."""
    from ..families import TOOLS
    from ..access import has_access_requirements
    levels = context.state.scope.levels if context.state.scope else None
    reads = []
    for tool in TOOLS:
        required = frozenset(tool.contract.required_access if tool.contract else ())
        if (has_access_requirements(context.guild, context.member.id, required)
                and (levels is None or required <= levels)):
            reads.append({'capability': tool.definition.name, 'summary': tool.definition.description})
    return {'source': 'registered family reads', 'reads': reads,
            'availability': 'Implemented read paths, not results. Each read checks current source availability and access; historical gaps remain explicit.'}


def current_channel(context):
    """Supply live room identity for model-selected clan policy reads, without routing prose."""
    channel = getattr(context.source_message, 'channel', None)
    if channel is None:
        return {}
    parent = getattr(channel, 'parent', None)
    category = getattr(channel, 'category', None) or getattr(parent, 'category', None)
    return {'channel_id': channel.id, 'name': getattr(channel, 'name', None),
            'topic': getattr(channel, 'topic', None),
            'parent': {'channel_id': parent.id, 'name': getattr(parent, 'name', None)} if parent else None,
            'category': {'channel_id': category.id, 'name': getattr(category, 'name', None)} if category else None}


@dataclass(frozen=True, slots=True)
class CompiledContext:
    prompt: str
    estimated_input_tokens: int
    input_content_bytes: int
    selected_request_ids: tuple[int, ...]
    omitted_turns: int
    exceeds_target: bool
    answer_context: str = ""


def select_recent_turns(turns: Sequence[ConversationTurn], *, token_budget: int,
                        render_turn=None) -> tuple[ConversationTurn, ...]:
    selected = []
    remaining = token_budget
    for turn in reversed(turns):
        cost = estimate_tokens((render_turn or _history_text)(turn)) + 1
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
    answer_local_context: str | None = None,
    social: bool = False,
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
    checkpoint = context.state.authorized_checkpoint
    instructions = [asdict(instruction) for instruction in context.state.authorized_instructions]
    from ..discord_actions.roles import role_change_access
    role_access = role_change_access(context) if not social else None
    own_identity = agent_identity(getattr(context, "bot", None), context.guild)
    command_overview = bot_command_overview(context) if not social else None
    requester_commands = requester_command_overview(context) if not social else None
    read_overview = bot_read_overview(context) if not social else None
    channel = current_channel(context)
    owner = getattr(context, "application_owner", None)
    if owner is not None:
        owner = guild_identity(owner, context.guild)
    for instruction in context.state.authorized_instructions:
        context.state.source_channels.add(instruction.source_channel_id)

    findings_by_turn = {}
    evidence = []
    for turn in candidates or ():
        if turn.record is None:
            continue
        for encoded in turn.record.evidence:
            finding = retained_finding(context, encoded)
            if finding is not None:
                findings_by_turn.setdefault(id(turn), []).append(json.loads(finding))
            else:
                evidence.append((turn, model_evidence(encoded)))
    def history_text(turn):
        rendered = _history_text(turn)
        findings = findings_by_turn.get(id(turn))
        if findings:
            value = json.loads(rendered)
            value['prior_findings'] = findings
            return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        return rendered
    carried = []

    def history_status(included: int, omitted: int) -> dict:
        status = {"included_turns": included, "older_retained_turns_available": omitted,
                  "history_may_be_incomplete": True}
        dropped = len(evidence) - len(carried)
        if evidence:
            status.update(included_evidence_records=len(carried), omitted_evidence_records=dropped)
        if dropped:
            status["evidence_omitted_from_requests"] = list(dict.fromkeys(
                turn.record.request_message_id for turn, _ in evidence[:dropped]))
            status["evidence_recovery"] = "Whole lookup records were omitted. Re-read them before making claims."
        return status

    def render(history: str, included: int, omitted: int, checkpoint_text: str = "") -> str:
        prompt = build_request_prompt(
            question=question, local_context=local_context, guild_name=context.guild.name,
            asker_name=context.member.display_name, asker_id=context.member.id,
            asked_at=context.source_message.created_at,
            conversation_history=history, history_status=history_status(included, omitted), report_manifest=manifest,
            task_instructions=instructions, history_checkpoint=checkpoint_text,
            agent_identity=asdict(own_identity) if own_identity is not None else None,
            application_owner=asdict(owner) if owner is not None else None,
            carried_evidence=carried,
            reply_context=context.state.reply_context,
            context_instructions=(
                *(("Canonical public command help for describing your supported tasks. When asked "
                    "what you do, combine these summaries with bot_read_overview.",) if not social else ()),
                *((FINDINGS_INSTRUCTION,) if findings_by_turn else ()),
            ),
        )
        prompt += "\n\n<current_channel>\n" + json.dumps(channel, ensure_ascii=False) + "\n</current_channel>"
        if social:
            return prompt
        return (prompt + "\n\n<bot_command_overview>\n"
                + json.dumps(command_overview, ensure_ascii=False) + "\n</bot_command_overview>"
                + "\n<requester_command_overview>\n"
                + json.dumps(requester_commands, ensure_ascii=False) + "\n</requester_command_overview>"
                + "\n<bot_read_overview>\n" + json.dumps(read_overview, ensure_ascii=False)
                + "\n</bot_read_overview>\n<role_change_access>\n"
                + json.dumps(role_access, ensure_ascii=False) + "\n</role_change_access>")

    selected = []
    checkpoint_text = ""
    if candidates is None:
        # Compatibility for service callers supplying their own authorized text.
        prompt = render(conversation_history, 0, 0)
        omitted = 0
    else:
        prompt = render("", 0, len(candidates))
        base = estimate_tokens(system_prompt) + estimate_tokens(tool_json) + REQUEST_OVERHEAD_ESTIMATE
        remaining = max(0, target_tokens - base - estimate_tokens(prompt))
        # The working target is soft. Reserve a bounded whole-turn allowance
        # even when the mandatory catalogue already exceeds it. Otherwise all
        # follow-ups lose their history before the first result is read.
        if base + estimate_tokens(prompt) >= target_tokens:
            remaining = min(8_000, target_tokens // 8)
        selected = list(select_recent_turns(candidates, token_budget=remaining, render_turn=history_text))
        pinned = pinned_turns(context, candidates)
        selected = [turn for turn in candidates
                    if any(turn is item for item in (*pinned, *selected))]
        omitted = len(candidates) - len(selected)
        checkpoint_text = ""
        if checkpoint is not None and checkpoint.covered_turn_count <= omitted:
            checkpoint_text = checkpoint.summary
        prompt = render(
            "\n".join(history_text(turn) for turn in selected), len(selected), omitted,
            checkpoint_text,
        )
        if checkpoint_text and base + estimate_tokens(prompt) > target_tokens:
            checkpoint_text = ""
            prompt = render(
                "\n".join(history_text(turn) for turn in selected), len(selected), omitted,
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
        prompt = render("\n".join(history_text(turn) for turn in selected),
                        len(selected), omitted, checkpoint_text)
        if len(carried) < len(evidence):
            partial = list(carried)
            carried[:] = [{"request_message_id": turn.record.request_message_id, "record": encoded}
                          for turn, encoded in evidence]
            complete = render("\n".join(history_text(turn) for turn in selected),
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
            candidate = render("\n".join(history_text(turn) for turn in selected),
                               len(selected), omitted, checkpoint_text)
            if base + estimate_tokens(candidate) > target_tokens:
                carried.pop(0)
                break
            prompt = candidate
        while carried and base + estimate_tokens(prompt) > target_tokens:
            carried.pop(0)
            prompt = render("\n".join(history_text(turn) for turn in selected),
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
    prior_findings = [{'request_message_id': turn.record.request_message_id, **finding}
                      for turn in selected for finding in findings_by_turn.get(id(turn), ())]
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
        answer_context=json.dumps({
            "request": question,
            "server": context.guild.name,
            "asker": {"member_id": context.member.id, "display_name": context.member.display_name},
            "role_change_access": role_access,
            "current_channel": channel,
            "asked_at": context.source_message.created_at.isoformat(),
            "agent": asdict(own_identity) if own_identity is not None else None,
            **({"bot_command_overview": {"commands": command_overview['commands']},
                "requester_command_overview": {"commands": requester_commands['commands']},
                "bot_read_overview": _member_read_overview(read_overview)} if not social else {}),
            "application_owner": asdict(owner) if owner is not None else None,
            "reply_context": context.state.reply_context,
            "history_checkpoint": checkpoint_text,
            "history_status": history_status(len(selected), omitted),
            "conversation_history": ("\n".join(_history_text(turn) for turn in selected)
                                     if candidates is not None else conversation_history),
            "task_instructions": instructions,
            **({'prior_findings': prior_findings,
                'findings_instruction': FINDINGS_INSTRUCTION} if prior_findings else {}),
            "nearby_conversation": (local_context if answer_local_context is None else answer_local_context),
        }, ensure_ascii=False),
    )


def _member_read_overview(overview):
    from ..engine.member_facts import read_topics
    return read_topics(overview)
