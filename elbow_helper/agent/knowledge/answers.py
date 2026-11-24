"""Channel-first answers and an explicitly requested, isolated private pass."""

from dataclasses import replace
import time

from elbow_helper.agent.safety.access import assert_answer_scope, require_evidence_access
from elbow_helper.agent.engine.models import AgentTurnState


def private_check_available(context):
    scope = context.state.scope
    return bool(
        not (context.private_pass or context.dm_delivery or scope is not None and scope.requester)
        and context.state.scope_audit.private_candidates
        and callable(context.state.private_answer_run)
    )


def private_check_offered(context):
    """Use the same eligibility for the offer text and its actual control."""
    return bool(context.state.scope_audit.offered and private_check_available(context))


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
    answer = await service.answer(question=question, context=private, standing_scope=local_context)
    await assert_answer_scope(private, answer)
    return answer, tuple(private.state.attachments), private
