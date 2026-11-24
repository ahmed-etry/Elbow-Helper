"""Tool execution and request state transitions."""

from __future__ import annotations

import logging

import asyncio
from dataclasses import fields, replace
from collections.abc import Mapping
import json
import time
from typing import Any
from elbow_helper.agent.engine.models import AgentRequestContext
from ..actions.contracts import ActionClass, ActionRefused, PreparedAction
from ..actions.discord_failure import DiscordFailure, discord_failure
from elbow_helper.agent.conversation.wording import ACTION_UNAVAILABLE
from elbow_helper.agent.safety.access import AgentAccessLost, LookupAccessDenied
from elbow_helper.agent.safety.access import has_lookup_access
from elbow_helper.agent.safety.access import require_evidence_access
from .capability_contract import CapabilityBindError
from .capability_contract import require_source_provenance
from ..research.member_provenance import member_target_issue
from . import budgets as limits
from .lookup_health import lookup_problem

LOGGER = logging.getLogger(__name__)
# A lookup that raised or broke its own result contract: the cause is in the log, not the data.
BOT_ERROR = "That lookup failed because of a bot error."


class AgentUnavailableError(RuntimeError):
    """Raised when an agent request cannot produce a final answer."""


async def execute_tool(
    *,
    name: str,
    handler: Any,
    arguments: Mapping[str, Any],
    capability_scope: Mapping[str, Any] | None = None,
    context: AgentRequestContext,
    timeout_seconds: float = limits.TOOL_TIMEOUT_SECONDS,
    action_class: ActionClass = ActionClass.READ,
) -> str:
    snapshot = tool_state_snapshot(context)
    started_at = time.monotonic()
    outcome = "failed"
    result_characters = 0
    private_before = set(context.state.scope_audit.private_candidates)
    failure_text = BOT_ERROR if action_class is ActionClass.READ else ACTION_UNAVAILABLE
    try:
        await require_evidence_access(context)
        if action_class is not ActionClass.READ and (
            issue := member_target_issue(context, arguments, capability_scope)
        ):
            outcome = "refused"
            return error_result(issue)
        required_access = frozenset((capability_scope or {}).get("required_access", ()))
        if required_access:
            if not has_lookup_access(context, required_access):
                outcome = "refused"
                return json.dumps({"error": ACTION_UNAVAILABLE,
                                   "required_access": sorted(required_access), "cause": "access_denied"})
        async with asyncio.timeout(timeout_seconds):
            payload = await handler(context, arguments)
        if not isinstance(payload, Mapping):
            raise CapabilityBindError("The lookup did not return a structured result.")
        refused_here = (context.state.scope_audit.private_candidates - private_before
                        or any("access:" + level in context.state.scope_audit.private_candidates
                               for level in payload.get("required_access", ())))
        denied = payload.get('cause') in ('scope_refused', 'access_denied')
        if refused_here and denied and "error" in payload:
            payload = {**payload, "available_privately": True}
        failed = "error" in payload or payload.get("flags", {}).get("status") == "failed"
        if not failed:
            context.state.required_access.update(required_access)
            require_source_provenance(
                capability_scope or {},
                arguments,
                context.state.source_channels,
                payload,
            )
        for source_id in context.state.source_channels:
            context.state.required_access.update(context.state.source_access.get(source_id, ()))
        record_report_provenance(context, snapshot["reports"])
        if action_class in (ActionClass.CHANGE, ActionClass.IRREVERSIBLE):
            capture_detail_provenance(context, snapshot, capability_scope or {})
        await require_evidence_access(context)
        outcome = "completed"
        context.state.feedback_lookup_problem |= lookup_problem(payload)
        content = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))
        result_characters = len(content)
        return content
    except LookupAccessDenied as error:
        outcome = "refused"
        restore_tool_state(context, snapshot)
        return json.dumps({"error": str(error), "required_access": sorted(error.requirements),
                           "cause": "access_denied",
                           "available_privately": bool(context.state.scope_audit.private_candidates)})
    except ActionRefused as error:
        outcome = "refused"
        restore_tool_state(context, snapshot)
        return json.dumps(error.result(), ensure_ascii=False)
    except CapabilityBindError:
        outcome = "unbound_source"
        restore_tool_state(context, snapshot)
        LOGGER.warning("Agent tool omitted source provenance: tool=%s", name)
        return error_result(failure_text)
    except AgentAccessLost:
        outcome = "access_lost"
        restore_tool_state(context, snapshot)
        raise
    except asyncio.CancelledError:
        outcome = "cancelled"
        restore_tool_state(context, snapshot)
        raise
    except asyncio.TimeoutError as error:
        restore_tool_state(context, snapshot)
        if (failure := discord_failure(error)) is not None:
            outcome = "discord_failed"
            return discord_failed_result(name, context, failure)
        outcome = "timed_out"
        LOGGER.warning(
            "Agent tool timed out: tool=%s invoker=%s",
            name,
            context.member.id,
        )
        return error_result("That lookup timed out.")
    except Exception as error:
        restore_tool_state(context, snapshot)
        if (failure := discord_failure(error)) is not None:
            outcome = "discord_failed"
            return discord_failed_result(name, context, failure)
        outcome = "failed"
        LOGGER.exception(
            "Agent tool failed: tool=%s invoker=%s",
            name,
            context.member.id,
        )
        return error_result(failure_text)
    except BaseException:
        outcome = "failed"
        restore_tool_state(context, snapshot)
        raise
    finally:
        if outcome not in ("completed", "cancelled", "access_lost"):
            context.state.feedback_lookup_problem = True
        LOGGER.info(
            (
                "Agent tool: tool=%s invoker=%s outcome=%s "
                "elapsed_ms=%s request=%s result_chars=%s"
            ),
            name,
            context.member.id,
            outcome,
            int((time.monotonic() - started_at) * 1_000),
            getattr(context.source_message, "id", None),
            result_characters,
        )


def capture_detail_provenance(context, snapshot, scope) -> None:
    """Keep preparation reads with the change rather than the public answer."""
    sources = frozenset(context.state.source_channels - snapshot["source_channels"])
    sources |= frozenset(scope.get("bound_source_channels", ()))
    access = frozenset(context.state.required_access - snapshot["required_access"])
    access |= frozenset(scope.get("required_access", ()))
    access |= frozenset(context.state.knowledge_title_access)
    for index in range(len(snapshot["proposed_changes"]), len(context.state.proposed_changes)):
        proposal = context.state.proposed_changes[index]
        if isinstance(proposal, PreparedAction):
            context.state.proposed_changes[index] = replace(
                proposal,
                preview=replace(
                    proposal.preview,
                    detail_sources=proposal.preview.detail_sources | sources,
                    detail_access=proposal.preview.detail_access | access,
                ),
            )
    context.state.source_channels = set(snapshot["source_channels"])
    context.state.required_access = set(snapshot["required_access"])


def record_report_provenance(
    context: AgentRequestContext,
    previous: Mapping[str, Any],
) -> None:
    """Bind new or replaced artifacts to all evidence access used so far."""
    for report_id, report in context.state.reports.items():
        if previous.get(report_id) is not report:
            context.state.report_sources[report_id] = frozenset(context.state.source_channels)
            context.state.report_access_requirements[report_id] = frozenset(
                context.state.required_access | context.state.knowledge_title_access
            )
    retained = set(context.state.reports)
    for provenance in (
        context.state.report_sources,
        context.state.report_access_requirements,
    ):
        for report_id in tuple(provenance):
            if report_id not in retained:
                provenance.pop(report_id)


def tool_state_snapshot(context: AgentRequestContext) -> dict[str, Any]:
    """Capture request-local state that a failed tool must not partially mutate."""
    return {
        field.name: value.copy() if isinstance(value, (dict, list, set)) else value
        for field in fields(context.state)
        for value in (getattr(context.state, field.name),)
    }


def restore_tool_state(
    context: AgentRequestContext,
    snapshot: Mapping[str, Any],
) -> None:
    """Restore request state after an unsuccessful lookup."""
    for name, saved in snapshot.items():
        current = getattr(context.state, name)
        if isinstance(current, (dict, set)):
            current.clear()
            current.update(saved)
        elif isinstance(current, list):
            current[:] = saved
        else:
            setattr(context.state, name, saved)


def error_result(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


def discord_failed_result(name: str, context: AgentRequestContext, failure: DiscordFailure) -> str:
    """Log the exception being handled, and say which kind of Discord failure it was."""
    LOGGER.exception(
        "Agent tool Discord failure: tool=%s invoker=%s kind=%s status=%s",
        name, context.member.id, failure.kind, failure.status,
    )
    return json.dumps({"error": failure.text, "cause": "source_unavailable"})
