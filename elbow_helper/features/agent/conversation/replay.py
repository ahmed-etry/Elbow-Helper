"""Replay retained answer text without planning or executing another request."""

from dataclasses import replace

from ..access import require_evidence_access
from ..wording import ACTION_EMPTY, ACTION_PRIVATE_ANSWER
from .context import retained_knowledge_sources


async def replay_answer(mode, context):
    """Return None for a fresh request; never regenerate a missing replay target."""
    if mode != "replay":
        return None
    await require_evidence_access(context)
    reference = getattr(context.source_message, "reference", None)
    target_id = getattr(reference, "message_id", None)
    # Use the complete retained history, not the filtered model excerpt. A hidden
    # latest turn must fail access checks rather than replay an older answer.
    turn = next((turn for turn in reversed(context.history)
                 if target_id is None and turn.retention_limited
                 or turn.record is not None and (target_id is None
                     or target_id == turn.record.request_message_id
                     or target_id in turn.record.reply_ids)), None)
    if turn is None or turn.record is None:
        return ACTION_EMPTY
    state = replace(
        context.state,
        source_channels=context.state.source_channels | set(turn.source_channels),
        required_access=context.state.required_access | set(turn.required_access),
    )
    await require_evidence_access(replace(context, state=state))
    context.state.source_channels.update(turn.source_channels)
    context.state.required_access.update(turn.required_access)
    context.state.knowledge_sources.update(retained_knowledge_sources(turn))
    record = turn.record
    delivered = record.delivered_answer
    # Legacy private notices contain no public answer to replay.
    notice = delivered.removeprefix(f"<@{record.member_id}> ")
    return ACTION_EMPTY if notice == ACTION_PRIVATE_ANSWER else delivered or ACTION_EMPTY
