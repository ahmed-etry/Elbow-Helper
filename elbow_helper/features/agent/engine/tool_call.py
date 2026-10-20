"""Tool execution and request state transitions."""

from __future__ import annotations

import logging

import asyncio
from dataclasses import fields, replace
from datetime import datetime, timezone
from collections.abc import Mapping, Sequence
import json
import time
from typing import Any
from elbow_helper.infrastructure.ai import AgentToolResult
from ..models import AgentRequestContext
from ..actions.contracts import ActionClass, ActionRefused, PreparedAction
from ..wording import ACTION_UNAVAILABLE
from ..access import AgentAccessLost, LookupAccessDenied
from ..access import has_access_requirements
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
    action_class: ActionClass = ActionClass.READ,
) -> str:
    snapshot = tool_state_snapshot(context)
    started_at = time.monotonic()
    outcome = "failed"
    result_characters = 0
    try:
        required_access = frozenset((capability_scope or {}).get("required_access", ()))
        if required_access:
            await require_evidence_access(context)
            if not has_access_requirements(context.guild, context.member.id, required_access):
                outcome = "refused"
                return json.dumps({"error": ACTION_UNAVAILABLE,
                                   "required_access": sorted(required_access)})
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
        if action_class in (ActionClass.CHANGE, ActionClass.IRREVERSIBLE):
            capture_detail_provenance(context, snapshot, capability_scope or {})
        await require_evidence_access(context)
        outcome = "completed"
        content = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))
        result_characters = len(content)
        return content
    except LookupAccessDenied as error:
        outcome = "refused"
        restore_tool_state(context, snapshot)
        return json.dumps({"error": str(error), "required_access": sorted(error.requirements)})
    except ActionRefused as error:
        outcome = "refused"
        restore_tool_state(context, snapshot)
        return error_result(str(error))
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
    except Exception:
        outcome = "failed"
        restore_tool_state(context, snapshot)
        LOGGER.exception(
            "Agent tool failed: tool=%s invoker=%s",
            name,
            context.member.id,
        )
        return error_result(ACTION_UNAVAILABLE if action_class is not ActionClass.READ
                            else "That lookup failed.")
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


def evidence_record(
    *,
    call_id: str,
    tool: str,
    arguments: Mapping[str, Any],
    result: str,
    raw_result_characters: int,
    result_complete: bool,
    model_result_content: str | None = None,
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
            **({"model_result": model_result_content} if model_result_content is not None else {}),
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
            "required_access": sorted(context.state.required_access | context.state.knowledge_title_access),
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
    target.state.knowledge_title_access.update(local.state.knowledge_title_access)
    target.state.private_knowledge.update(local.state.private_knowledge)
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
        if ((attachment.report_id or "").startswith("spreadsheet:")
                and attachment in previous["attachments"]):
            continue
        if attachment not in target.state.attachments:
            if (attachment.report_id or "").startswith("spreadsheet:"):
                target.state.attachments[:] = [
                    item for item in target.state.attachments
                    if not (item.report_id or "").startswith("spreadsheet:")
                ]
            target.state.attachments.append(attachment)
    target.state.history_status.update(local.state.history_status)
    if local.state.gif_url != previous["gif_url"]:
        target.state.gif_url = local.state.gif_url
    for name in ("reply_to", "ping_author"):
        if getattr(local.state, name) != previous[name]:
            setattr(target.state, name, getattr(local.state, name))
    if local.state.authorized_history is not None:
        target.state.authorized_history = local.state.authorized_history
    if local.state.authorized_checkpoint is not None:
        target.state.authorized_checkpoint = local.state.authorized_checkpoint
    target.state.working = local.state.working
    target.state.authorized_instructions = local.state.authorized_instructions
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
    clash = _clash_size_limit(content, limit)
    if clash is not None:
        return clash
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


def _clash_size_limit(content: str, limit: int) -> str | None:
    """Do not expose an arbitrary JSON excerpt as part of a Clash dataset."""
    try:
        payload = json.loads(content)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    live = payload if payload.get("source") == "clash_api" else payload.get(
        "live_battle_logs", {}).get("result", {})
    if not isinstance(live, dict) or live.get("source") != "clash_api":
        return None
    from copy import deepcopy
    from ..datasets.clash import summary, collection_counts

    def records(value):
        if isinstance(value, dict):
            if set(value) == {"columns", "rows"}:
                return [records(dict(zip(value["columns"], row))) for row in value["rows"]]
            return {key: records(item) for key, item in value.items()}
        return [records(item) for item in value] if isinstance(value, list) else value

    # Preserve the wrapper (ranked reads may contain both stored and live data).
    bounded = records(deepcopy(payload))
    target = bounded if live is payload else bounded["live_battle_logs"]["result"]
    items = target.get("items", [])
    totals = [{"tag": item["tag"], "collections": {
        path: {"returned_rows": 0, "total_rows": count["total_rows"]}
        for path, count in item.get("row_counts", {}).items()
    }} for item in items]
    target.update(truncated=True, flags={**target.get("flags", {}),
                                         "status": "partial", "truncated": True})

    def encode():
        return json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))

    # Full profiles can be large. Keep every completed account's compact facts
    # before considering a smaller page. Missing fields are explicit, never an
    # arbitrary JSON excerpt passed off as a complete profile.
    for item in items:
        original = item.get("data")
        if item.get("status") == "ok" and isinstance(original, dict):
            projected = summary(live.get("kind"), original)
            if projected != original:
                item.update(data=projected, projection="summary", truncated=True)
                counts = collection_counts(projected, original)
                item["row_counts"] = {path: {
                    "returned_rows": counts.get(path, {}).get("returned_rows", 0),
                    "total_rows": count["total_rows"],
                } for path, count in item.get("row_counts", {}).items()}
    target["projection"] = "summary"
    encoded = encode()
    if len(encoded) <= limit:
        return encoded

    # Preserve the league/count facts of the entire cohort when even summaries
    # are too large. Do not lose a zero-trophy account's ranked league.
    if live.get("kind") in ("player", "clan"):
        keys = ("tag", "name", "townHallLevel", "leagueTier", "league", "trophies", "bestTrophies") \
            if live["kind"] == "player" else ("tag", "name", "members")
        for item in items:
            if isinstance(item.get("data"), dict):
                item["data"] = {key: value for key, value in item["data"].items() if key in keys}
                item.update(projection="facts", truncated=True)
                item.pop("row_counts", None)
        target["projection"] = "facts"
        encoded = encode()
        if len(encoded) <= limit:
            return encoded

    # A bounded prefix is still useful. The remainder can be read using the
    # normal tags argument and request cache, without repeating completed I/O.
    target["items"] = []
    target["total_accounts"] = len(items)
    target["next_tags"] = [item["tag"] for item in items]
    target["row_counts"] = totals
    best = encode()
    for index, item in enumerate(items):
        target["items"].append(item)
        target["next_tags"] = [row["tag"] for row in items[index + 1:]]
        target["row_counts"] = totals[index + 1:]
        encoded = encode()
        if len(encoded) > limit:
            break
        best = encoded
    if len(best) <= limit:
        return best
    encoded = json.dumps({"truncated": True, "returned_rows": 0,
                          "total_rows": sum(count["total_rows"] for item in totals
                                            for count in item["collections"].values())},
                         separators=(",", ":"))
    return encoded if len(encoded) <= limit else ""



def error_result(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)
