"""Channel-first answers and an explicitly requested, isolated private pass."""

from dataclasses import replace
import json
import re
import time

from ..access import assert_answer_scope, require_evidence_access
from ..models import AgentTurnState
from ..wording import ACTION_PRIVATE_NOTE, AGENT_SCOPE_EMPTY, FAILURE_MESSAGE


CHANNEL_FIRST_INSTRUCTION = (
    "Answer only from evidence returned within this destination's scope. A lookup that "
    "is unavailable here supplies no evidence. Never infer or repeat excluded contents "
    "or titles. If an available_privately flag or a scope-refused lookup would materially "
    "change the answer, return exactly {\"needs_private_evidence\":true,\"answer\":"
    "\"your useful answer from the evidence available here\"}. Otherwise answer normally. "
    "Keep the answer useful when evidence is excluded: answer the supported parts, "
    "and state plainly which requested information is unavailable in this channel. "
    "Never suggest that a source you cannot read here contains nothing. "
    "A scope refusal is not a failed request. Do not give a generic failure, ask to "
    "retry, describe lookup/access mechanics, or say 'outside what I can see here'. "
    "The system alone adds any private-answer offer and its control. Never include that "
    "offer, a suggestion to continue privately, or instructions for using its control in "
    "your prose, including inside the answer field. Signal materiality only through "
    "needs_private_evidence; otherwise give the supported answer here. A private check "
    "runs only after the asker presses its button. "
    "Evidence returned in this request is already permitted for this destination: show "
    "the requested findings here, including member names and dates. Never withhold it "
    "because it is sensitive elsewhere or a previous reply called it private. "
    "A private-only claim requires an actual private-check offer backed by excluded "
    "evidence the asker may access; the JSON signal alone cannot create that offer. "
    "State restrictions using the real member-facing roles from knowledge; never describe "
    "refused reads, visibility checks or internal groups. Do not narrate the private-check "
    "button or promise what a later request will find. Otherwise return plain reply text, "
    "not an answer envelope. When the private-evidence signal is needed, return valid JSON "
    "with escaped newlines and quotes inside answer."
)


_PRIVATE_CLAIM = re.compile(
    r"\bprivate[- ]only\b"
    r"|\bonly\s+(?:(?:open|available|visible|shown|shared|viewable|accessible)\s+)?privately\b"
    r"|\bonly\s+(?:in|via)\s+(?:a\s+)?(?:private\s+(?:reply|message)|dms?)\b"
    r"|\b(?:open|view|check)\s+(?:it|them|the\s+(?:result|list|names|dates))\s+privately\b"
    r"|\bi\s+can\s+(?:only\s+)?(?:show|share|send|check)\b[^.!?\n]{0,60}\bprivately\b",
    re.IGNORECASE,
)

SCOPE_REPLY_INSTRUCTION = (
    "The reply claims a private result without a deliverable private-check offer. "
    "A needs_private_evidence signal is not proof of a restriction. Show the requested "
    "findings already returned for this destination, including names and dates, in the "
    "requested order and format. Do not invent or relax permissions. If evidence was "
    "actually excluded, answer the supported parts and name only the verified access "
    "restriction. Use needs_private_evidence=true only for relevant excluded evidence "
    "the asker can check through the available button. Otherwise answer normally. "
    "Do not claim a hidden result exists or promise a later private reply."
)


MEMBER_REPLY_INSTRUCTION = (
    "Give the requested facts directly in ordinary clan-chat words. "
    "Preserve supported facts, names, dates and source links. Describe yourself as I, "
    "including when asked about your own behavior. Express a real gap as the specific "
    "thing you do not know, without framing it around evidence, records, documentation "
    "or your command list. Do not infer that an automation is absent or manual just "
    "because command help or an unrelated workflow does not describe it. Do not narrate searches, "
    "empty queries, room visibility, refused reads or where you can look. For example, "
    "say 'The promotion ticket's contents aren't available here', rather than describing "
    "whether its room is visible. An empty search means the requested discussion is not "
    "known, not that it never happened. Do not invent "
    "hidden discussion or promise another lookup. Preserve a needed private-evidence "
    "signal only when relevant excluded evidence has a usable private-check offer. "
    "Otherwise return the useful answer directly. Lead with the requested person or fact; "
    "do not preface it with an unasked missing role, label or search result."
)

# These checks inspect generated replies only, never the asker's request.
_RETRIEVAL_NARRATION = re.compile(
    r"\b(?:search|lookup|query)\b[^.!?\n]{0,140}\b(?:empty|no (?:results|matches)|nothing|comes? back|returned|found)\b"
    r"|\bi\s+(?:searched|queried|looked\s+(?:up|through))\b"
    r"|\b(?:ticket|room|channel|thread)\b[^.!?\n]{0,80}\b(?:not|isn['’]t|aren['’]t)\s+(?:visible|readable|accessible)\b"
    r"|\b(?:can['’]t|cannot|couldn['’]t)\s+(?:see|read|access)\b[^.!?\n]{0,80}\b(?:room|channel|thread)\b"
    r"|\b(?:outside\s+what|in\s+what|nothing[^.!?\n]{0,40})\s+i\s+can\s+(?:see|read)\b",
    re.IGNORECASE,
)
_ENVELOPE_KEYS = frozenset({"answer", "needs_private_evidence", "needs_restricted_knowledge"})


def _answer_parts(answer):
    """Decode reply wrappers without accepting new scope flags or schema fields."""
    prose, needs_private = answer or "", False
    for _ in range(8):
        fenced = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", prose, re.DOTALL)
        value = fenced.group(1) if fenced else prose
        try:
            signal = json.loads(value, strict=False)
        except (TypeError, ValueError):
            # Even a truncated wrapper with answer first must not reach Discord.
            if (value.lstrip().startswith("{") and re.search(
                    r'"(?:answer|needs_private_evidence|needs_restricted_knowledge)"\s*:', value)):
                raise ValueError("Malformed answer envelope") from None
            return prose, needs_private
        if not isinstance(signal, dict) or not _ENVELOPE_KEYS.intersection(signal):
            return prose, needs_private
        if (set(signal) not in ({"answer"}, {"answer", "needs_private_evidence"})
                or not isinstance(signal.get("answer"), str)
                or ("needs_private_evidence" in signal and type(signal["needs_private_evidence"]) is not bool)):
            raise ValueError("Invalid answer envelope")
        needs_private |= signal.get("needs_private_evidence", False)
        prose = signal["answer"].strip() or AGENT_SCOPE_EMPTY
    raise ValueError("Nested answer envelope limit")


def _private_check_available(context):
    scope = context.state.scope
    return bool(
        not (context.private_pass or context.dm_delivery or scope is not None and scope.requester)
        and context.state.scope_audit.private_candidates
        and callable(context.state.private_answer_run)
    )


def private_check_offered(context):
    """Use the same eligibility for the offer text and its actual control."""
    return bool(context.state.scope_audit.offered and _private_check_available(context))


def scope_reply_issue(context, answer, *, check_narration=True):
    """Check generated withholding claims against recorded scope and delivery state."""
    try:
        prose, needs_private = _answer_parts(answer)
    except ValueError:
        return ""  # scoped_answer replaces invalid envelopes with safe prose.
    # Command adapters can already supply a real requester-only result control.
    # Its fixed notice is not a model-invented restriction on research evidence.
    if any(item.visibility == "private" and (item.private_parts or item.attachments or item.private_panel)
           for item in context.state.outcomes):
        prose = prose.replace(ACTION_PRIVATE_NOTE, "")
    prose = re.sub(r"```.*?```", "", prose, flags=re.DOTALL)
    prose = "\n".join(line for line in prose.splitlines() if not line.lstrip().startswith(">"))
    prose = prose.replace("*", "").replace("`", "")
    if check_narration and _RETRIEVAL_NARRATION.search(prose):
        return MEMBER_REPLY_INSTRUCTION
    private_claim = any(not re.search(r"\b(?:not|never|isn['’]t|aren['’]t)\s*$", prose[:match.start()],
                                      re.IGNORECASE) for match in _PRIVATE_CLAIM.finditer(prose))
    # A materiality flag cannot establish a restriction. If no private check
    # is available, scoped_answer can still deliver the already authorized
    # factual prose. Only an actual withholding claim needs a rewrite.
    if private_claim and not (
        _private_check_available(context) and (needs_private or context.state.scope_audit.offered)
    ):
        return SCOPE_REPLY_INSTRUCTION
    return ""


def scoped_answer(context, answer):
    """Unwrap the materiality signal without opening any excluded evidence."""
    audit = context.state.scope_audit
    if answer == FAILURE_MESSAGE and context.state.provider_failed and not context.state.has_read_answer:
        return answer
    if not answer and (context.state.gif_url or context.state.attachments
                       or context.state.reply_delivery.posts
                       or context.state.reply_delivery.delivery.message_ids):
        return ""
    from ..engine.unfinished import unfinished_answer
    evidence = context.state.grounding_evidence
    fallback = unfinished_answer(context.state.request_text, evidence)
    if audit.excluded and (not answer or answer.strip() == FAILURE_MESSAGE):
        return fallback
    try:
        prose, needs_private = _answer_parts(answer)
    except ValueError:
        return fallback
    if (needs_private and not getattr(context, "private_pass", False)
            and audit.private_candidates):
        audit.offered = True
    return fallback if not prose or prose in (FAILURE_MESSAGE, AGENT_SCOPE_EMPTY) else prose


async def private_answer_pass(service, context, question, local_context):
    """Fresh tools, evidence and model session at the asker's current clearance."""
    await require_evidence_access(context)
    from ..conversation.reply_context import causal_visible_history
    private = replace(
        context, private_pass=True, state=AgentTurnState(), history=causal_visible_history(context),
        send_reply=None, stop_typing=None, operator_debug=None,
        deadline_monotonic=time.monotonic() + 300,
        disclosure_thread_members={}, transcript_archive=None,
    )
    private.state.source_channels.add(context.source_message.channel.id)
    answer = await service.answer(question=question, local_context=local_context, context=private)
    await assert_answer_scope(private, answer)
    return answer, tuple(private.state.attachments), private
