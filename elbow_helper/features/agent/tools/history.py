"""Permission-checked retrieval from the current conversation's retained turns."""

from __future__ import annotations

import json
from typing import Any, Mapping

from elbow_helper.infrastructure.ai import AgentToolDefinition

from ..access import accessible_message_channel, require_evidence_access
from ..conversation.state import ConversationTurn
from ..models import AgentRequestContext, RegisteredAgentTool


HISTORY_EXCERPT_CHARACTERS = 2_000
HISTORY_ENTRY_JSON_CHARACTERS = 5_000


def history_tools() -> tuple[RegisteredAgentTool, ...]:
    return (RegisteredAgentTool(
        AgentToolDefinition(
            name="read_conversation_history",
            description=(
                "Read earlier turns in this conversation when the supplied history is insufficient. "
                "Search by keyword or retrieve a request message ID. Only turns whose source "
                "channels you can still access are returned. Results are paginated; retained "
                "history may be incomplete."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "maxLength": 200},
                    "request_message_id": {"type": "integer", "minimum": 1},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 5},
                    "content_offset": {"type": "integer", "minimum": 0},
                },
                "additionalProperties": False,
            },
        ),
        read_conversation_history,
    ),)


def _document(turn: ConversationTurn, *, stale_knowledge: bool = False) -> str:
    record = turn.record
    if record is None:
        document = turn.text
        return (
            "HISTORICAL KNOWLEDGE WARNING: policy cited in this retained turn "
            "has changed, expired or retired; search approved knowledge again.\n"
            f"{document}"
            if stale_knowledge else document
        )
    # Undelivered generated text is not conversation evidence.
    document = json.dumps({
        "question": record.question,
        "answer": record.delivered_answer,
        "delivery_complete": record.delivery_complete,
        "delivery_unknown": record.delivery_unknown,
        "local_context": record.local_context,
        "lookup_results": record.evidence,
        "report_ids": record.report_ids,
        "reply_ids": record.reply_ids,
    }, ensure_ascii=False)
    return (
        "HISTORICAL KNOWLEDGE WARNING: policy cited in this retained turn has "
        "changed, expired or retired; search approved knowledge again.\n"
        f"{document}"
        if stale_knowledge else document
    )


def _excerpt(turn: ConversationTurn, document: str, start: int) -> dict[str, Any]:
    record = turn.record
    end = min(len(document), start + HISTORY_EXCERPT_CHARACTERS)
    result = {
        "request_message_id": record.request_message_id if record else None,
        "member_id": record.member_id if record else None,
        "created_at": record.created_at if record else None,
        "retention_limited": turn.retention_limited or record is None,
        "content_offset": start,
        "content": document[start:end],
        "next_content_offset": end if end < len(document) else None,
    }
    # Escapes can expand the serialized result. Keep each envelope intact.
    while len(json.dumps(result, ensure_ascii=False)) > HISTORY_ENTRY_JSON_CHARACTERS:
        end = start + (end - start) // 2
        result["content"] = document[start:end]
        result["next_content_offset"] = end if end < len(document) else None
    return result


async def read_conversation_history(
    context: AgentRequestContext, arguments: Mapping[str, Any],
) -> Mapping[str, Any]:
    await require_evidence_access(context)
    query = str(arguments.get("query") or "").casefold()
    request_id = arguments.get("request_message_id")
    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", 3)
    access: dict[int, bool] = {}
    matches: list[tuple[ConversationTurn, str]] = []
    # The request receives an immutable turn tuple for this conversation only.
    for turn in reversed(context.history):
        if request_id is not None and (
            turn.record is None or turn.record.request_message_id != request_id
        ):
            continue
        for channel_id in turn.source_channels:
            if channel_id not in access:
                access[channel_id] = await accessible_message_channel(context, channel_id) is not None
        if not all(access[channel_id] for channel_id in turn.source_channels):
            continue
        stale_knowledge = bool(
            set(turn.knowledge_refs) & context.state.stale_knowledge_refs
            or (
                turn.record is not None
                and set(turn.record.report_ids)
                & context.state.stale_knowledge_report_ids
            )
        )
        document = _document(turn, stale_knowledge=stale_knowledge)
        if query and query not in document.casefold():
            continue
        matches.append((turn, document))
    results = []
    for turn, document in matches[offset:offset + limit]:
        start = min(arguments.get("content_offset", 0), len(document))
        if query and "content_offset" not in arguments:
            # Search excerpts start near the hit; explicit offsets read sequentially.
            folded = document.casefold()
            match = folded.find(query)
            # Casefold can change length. Convert its index back to the original.
            position = 0
            for index, character in enumerate(document):
                if position >= match:
                    start = max(0, index - 200)
                    break
                position += len(character.casefold())
        results.append(_excerpt(turn, document, start))
        context.state.source_channels.update(turn.source_channels)
    await require_evidence_access(context)
    return {
        "results": results,
        "matching_retained_turns": len(matches),
        "next_offset": offset + limit if offset + limit < len(matches) else None,
        "history_may_be_incomplete": True,
    }
