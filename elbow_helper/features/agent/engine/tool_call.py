"""Tool execution and request state transitions."""

from __future__ import annotations

import logging

import asyncio
from dataclasses import fields, replace
from datetime import datetime, timezone
from collections.abc import Mapping, Sequence
import json
import sqlite3
import time
from typing import Any
import discord
from elbow_helper.infrastructure.ai import AgentToolResult
from ..models import AgentRequestContext
from ..access import AgentAccessLost
from ..disclosure import AgentDisclosureDenied
from ..access import require_access_requirements
from ..disclosure import require_destination_access
from ..access import require_evidence_access
from .capability_contract import CapabilityBindError
from .capability_contract import require_source_provenance
from ..reports.base import retain_reports
from . import budgets as limits

LOGGER = logging.getLogger(__name__)


async def execute_tool(
    *,
    name: str,
    handler: Any,
    arguments: Mapping[str, Any],
    capability_scope: Mapping[str, Any] | None = None,
    context: AgentRequestContext,
    timeout_seconds: float = limits.TOOL_TIMEOUT_SECONDS,
) -> str:
    snapshot = tool_state_snapshot(context)
    started_at = time.monotonic()
    outcome = "failed"
    result_characters = 0
    try:
        required_access = frozenset((capability_scope or {}).get("required_access", ()))
        if required_access:
            require_access_requirements(
                context.guild,
                context.member.id,
                required_access,
            )
        async with asyncio.timeout(timeout_seconds):
            payload = await handler(context, arguments)
        if not isinstance(payload, Mapping):
            raise CapabilityBindError("The lookup did not return a structured result.")
        failed = "error" in payload or payload.get("flags", {}).get("status") == "failed"
        if not failed:
            context.state.required_access.update(required_access)
            require_source_provenance(
                capability_scope or {},
                arguments,
                context.state.source_channels,
                payload,
            )
        record_report_provenance(context, snapshot["reports"])
        sources = await require_evidence_access(context)
        await require_destination_access(context, sources)
        outcome = "completed"
        content = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))
        result_characters = len(content)
        return content
    except AgentDisclosureDenied:
        outcome = "disclosure_denied"
        restore_tool_state(context, snapshot)
        return error_result("That source cannot be shared in this channel.")
    except CapabilityBindError:
        outcome = "unbound_source"
        restore_tool_state(context, snapshot)
        LOGGER.warning("Agent tool omitted source provenance: tool=%s", name)
        return error_result("That lookup failed.")
    except AgentAccessLost:
        outcome = "access_lost"
        restore_tool_state(context, snapshot)
        raise
    except asyncio.CancelledError:
        outcome = "cancelled"
        restore_tool_state(context, snapshot)
        raise
    except asyncio.TimeoutError:
        outcome = "timed_out"
        restore_tool_state(context, snapshot)
        LOGGER.warning(
            "Agent tool timed out: tool=%s invoker=%s",
            name,
            context.member.id,
        )
        return error_result("That lookup timed out.")
    except (
        discord.DiscordException,
        KeyError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ):
        outcome = "failed"
        restore_tool_state(context, snapshot)
        LOGGER.exception(
            "Agent tool failed: tool=%s invoker=%s",
            name,
            context.member.id,
        )
        return error_result("That lookup failed.")
    except BaseException:
        outcome = "failed"
        restore_tool_state(context, snapshot)
        raise
    finally:
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


def evidence_record(
    *,
    call_id: str,
    tool: str,
    arguments: Mapping[str, Any],
    result: str,
    raw_result_characters: int,
    result_complete: bool,
    capability_scope: Mapping[str, Any] | None = None,
    context: AgentRequestContext,
) -> str:
    """Keep the model view's exact scope and access provenance for reuse."""
    try:
        payload = json.loads(result)
    except ValueError:
        payload = None
    if isinstance(payload, dict) and (
        "error" in payload or payload.get("flags", {}).get("status") == "failed"
    ):
        status = "error"
    elif not result_complete or (
        isinstance(payload, dict)
        and (payload.get("truncated") or payload.get("flags", {}).get("status") == "partial")
    ):
        status = "partial"
    else:
        status = "complete"
    return json.dumps(
        {
            "call_id": call_id,
            "tool": tool,
            "arguments": arguments,
            "result": result,
            "result_status": status,
            "raw_result_characters": raw_result_characters,
            "result_complete": status == "complete",
            "capability_scope": dict(
                capability_scope
                or {
                    "precision": "schema_only",
                    "capability": tool,
                }
            ),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "source_channels": sorted(context.state.source_channels),
            "required_access": sorted(context.state.required_access),
        },
        ensure_ascii=False,
    )


def record_report_provenance(
    context: AgentRequestContext,
    previous: Mapping[str, Any],
) -> None:
    """Bind new or replaced artifacts to all evidence access used so far."""
    for report_id, report in context.state.reports.items():
        if previous.get(report_id) is not report:
            context.state.report_sources[report_id] = frozenset(context.state.source_channels)
            context.state.report_access_requirements[report_id] = frozenset(
                context.state.required_access
            )
    retained = set(context.state.reports)
    for provenance in (
        context.state.report_sources,
        context.state.report_access_requirements,
    ):
        for report_id in tuple(provenance):
            if report_id not in retained:
                provenance.pop(report_id)


def clone_tool_state(state):
    return replace(
        state,
        **{
            field.name: value.copy()
            for field in fields(state)
            if isinstance(value := getattr(state, field.name), (dict, list, set))
        },
    )


def merge_tool_state(
    target: AgentRequestContext, local: AgentRequestContext, previous: Mapping[str, Any]
) -> None:
    additions = tuple(
        report
        for key, report in local.state.reports.items()
        if previous["reports"].get(key) is not report
    )
    if additions:
        retain_reports(target.state.reports, additions)
    target.state.source_channels.update(local.state.source_channels)
    target.state.required_access.update(local.state.required_access)
    for name in ("reports", "report_sources", "report_access_requirements"):
        destination = getattr(target.state, name)
        selected = getattr(local.state, name)
        for key in previous[name].keys() - selected.keys():
            if destination.get(key) == previous[name][key]:
                destination.pop(key, None)
        for key, value in selected.items():
            if previous[name].get(key) is not value and previous[name].get(key) != value:
                destination[key] = value
    retained = set(target.state.reports)
    for provenance in (target.state.report_sources, target.state.report_access_requirements):
        for key in tuple(provenance):
            if key not in retained:
                provenance.pop(key)
    target.state.preserved_reports.update(local.state.preserved_reports)
    target.state.preserved_report_sources.update(local.state.preserved_report_sources)
    target.state.preserved_report_access_requirements.update(
        local.state.preserved_report_access_requirements
    )
    for attachment in local.state.attachments:
        if attachment not in target.state.attachments:
            target.state.attachments.append(attachment)
    target.state.history_status.update(local.state.history_status)
    if local.state.authorized_history is not None:
        target.state.authorized_history = local.state.authorized_history
    if local.state.authorized_checkpoint is not None:
        target.state.authorized_checkpoint = local.state.authorized_checkpoint
    target.state.working = local.state.working
    target.state.authorized_instructions = local.state.authorized_instructions
    target.state.stale_knowledge_report_ids.update(local.state.stale_knowledge_report_ids)
    target.state.stale_knowledge_refs.update(local.state.stale_knowledge_refs)
    target.state.outcomes.extend(
        local.state.outcomes[len(previous["outcomes"]) :]
    )
    target.state.proposed_changes.extend(
        local.state.proposed_changes[len(previous["proposed_changes"]) :]
    )


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


async def discard_unpublished_results(
    context: AgentRequestContext,
    snapshot: Mapping[str, Any],
    results: Sequence[AgentToolResult],
) -> list[AgentToolResult]:
    """Discard unsent evidence; never continue with revoked model context."""
    restore_tool_state(context, snapshot)
    # This includes rollout eligibility, the request channel, earlier evidence and
    # role requirements. Loss of anything already in the prompt still aborts.
    await require_evidence_access(context)
    LOGGER.warning(
        "Agent unpublished evidence discarded: " "request=%s invoker=%s results=%s",
        getattr(context.source_message, "id", None),
        context.member.id,
        len(results),
    )
    return [
        AgentToolResult(result.call_id, error_result("That lookup failed.")) for result in results
    ]


def bound_tool_result(content: str, limit: int) -> str:
    if limit < 2:
        return ""
    if len(content) <= limit:
        return content
    # JSON escaping can expand an excerpt; measure the encoded envelope itself.
    low, high = 0, len(content)
    result = "{}"
    while low <= high:
        midpoint = (low + high) // 2
        candidate = json.dumps(
            {"truncated": True, "result_excerpt": content[:midpoint]}, ensure_ascii=False
        )
        if len(candidate) <= limit:
            result = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return result


def error_result(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)
