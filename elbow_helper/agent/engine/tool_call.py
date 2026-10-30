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
from ..access import has_lookup_access
from ..access import require_evidence_access
from .capability_contract import CapabilityBindError
from .capability_contract import require_source_provenance
from ..reports.base import retain_reports
from ..research.member_provenance import member_target_issue
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
    private_before = set(context.state.scope_audit.private_candidates)
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
                                   "required_access": sorted(required_access)})
        async with asyncio.timeout(timeout_seconds):
            payload = await handler(context, arguments)
        if not isinstance(payload, Mapping):
            raise CapabilityBindError("The lookup did not return a structured result.")
        refused_here = (context.state.scope_audit.private_candidates - private_before
                        or any("access:" + level in context.state.scope_audit.private_candidates
                               for level in payload.get("required_access", ())))
        denied = (payload.get('cause') in ('scope_refused', 'access_denied')
                  or payload.get('required_access')
                  or 'access_denied' in payload.get('flags', {}).get('limits', ()))
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
        content = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))
        result_characters = len(content)
        return content
    except LookupAccessDenied as error:
        outcome = "refused"
        restore_tool_state(context, snapshot)
        return json.dumps({"error": str(error), "required_access": sorted(error.requirements),
                           "available_privately": bool(context.state.scope_audit.private_candidates)})
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
    plan: Mapping[str, Any] | None = None,
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
            **({'plan': plan} if plan is not None else {}),
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
            "knowledge_sources": sorted(context.state.knowledge_sources),
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
    target.state.source_access.update(local.state.source_access)
    target.state.required_access.update(local.state.required_access)
    target.state.help_requester_access.update(local.state.help_requester_access)
    target.state.knowledge_title_access.update(local.state.knowledge_title_access)
    target.state.knowledge_sources.update(local.state.knowledge_sources)
    target.state.history_cursors.update(local.state.history_cursors)
    target.state.resolved_members.update(local.state.resolved_members)
    target.state.candidate_member_ids.update(local.state.candidate_member_ids)
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
        target.state.gif_attachment = local.state.gif_attachment
        target.state.gif_item_id = local.state.gif_item_id
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
    try:
        payload = json.loads(content)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        from ..families.paging import bound_family_page
        family = bound_family_page(payload, limit)
        if family is not None:
            return family
    clash = _clash_size_limit(content, limit)
    if clash is not None:
        return clash
    return _record_size_limit(content, limit)


def _record_size_limit(content: str, limit: int) -> str:
    """Keep complete records, including compact table rows, within the budget."""
    try:
        payload = json.loads(content)
    except ValueError:
        payload = None
    if isinstance(payload, list):
        payload = {"records": payload}
    if not isinstance(payload, dict):
        payload = {}
    payload["truncated"] = True

    def encode():
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def collections(value, path=""):
        if not isinstance(value, dict):
            return
        for key, child in value.items():
            if key == "truncated_collections":
                continue
            child_path = f"{path}.{key}" if path else key
            if isinstance(child, list) and key != "columns" and child:
                yield value, key, child_path
            elif isinstance(child, dict):
                yield from collections(child, child_path)

    def retain(rows, count, owner):
        columns = owner.get('columns', ())
        index = columns.index('message_id') if isinstance(columns, (list, tuple)) and 'message_id' in columns else None
        def identity(row):
            return (row.get('message_id') if isinstance(row, dict) else
                    row[index] if index is not None and isinstance(row, (list, tuple)) and len(row) > index else None)
        ids = [identity(row) for row in rows]
        if ids and all(str(value).isdecimal() for value in ids):
            # Keep later decisions in dated Discord evidence. Select by the
            # immutable snowflake, then preserve the original display order.
            selected = set(sorted(range(len(rows)), key=lambda i: int(ids[i]), reverse=True)[:count])
            return [row for i, row in enumerate(rows) if i in selected]
        return rows[:count]

    while len(encode()) > limit:
        candidates = list(collections(payload))
        if not candidates:
            # Even metadata or one scalar can exceed the entire allowance.
            # Expose no partial record or raw JSON prefix as usable evidence.
            empty = '{"truncated":true,"returned_records":0}'
            return empty if len(empty) <= limit else "{}"
        owner, key, path = max(candidates, key=lambda item: len(json.dumps(item[0][item[1]], ensure_ascii=False)))
        rows = owner[key]
        omitted = payload.setdefault("truncated_collections", {})
        omitted.setdefault(path, {"total_records": len(rows)})
        # Share pressure across large collections instead of emptying one
        # discussion to make room for another equally large discussion.
        kept = len(rows) // 2
        owner[key] = retain(rows, kept, owner)
        omitted[path]['returned_records'] = kept
        if len(encode()) > limit:
            continue
        low, high = kept, len(rows) - 1
        while low <= high:
            midpoint = (low + high) // 2
            owner[key] = retain(rows, midpoint, owner)
            omitted[path]["returned_records"] = midpoint
            if len(encode()) <= limit:
                kept = midpoint
                low = midpoint + 1
            else:
                high = midpoint - 1
        owner[key] = retain(rows, kept, owner)
        omitted[path]["returned_records"] = kept
    return encode()


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
    from ..datasets.clash import summary, collection_counts, omitted_fields, select_fields
    from ..plan.results import compact_result

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
    totals = [{"tag": item["tag"], "status": item["status"], "collections": {
        path: {"returned_rows": 0, "total_rows": count["total_rows"]}
        for path, count in item.get("row_counts", {}).items()
    }} for item in items]
    target.update(truncated=True, flags={**target.get("flags", {}),
                                         "status": "partial", "truncated": True})
    # Fetch outcomes survive every projection/page. Missing evidence is not a
    # failed API read, including for tags whose entire data is omitted below.
    target["read_statuses"] = {item["tag"]: item["status"] for item in items}
    target["flags"]["limits"] = sorted(set(target["flags"].get("limits", ())) | {"evidence_size_limit"})

    def encode():
        return json.dumps(compact_result(bounded), ensure_ascii=False, separators=(",", ":"))

    def project(item, projected, mode):
        original = item["data"]
        missing = omitted_fields(original, projected)
        item.update(data=projected, projection=mode, truncated=True)
        item["omitted_fields"] = sorted(set(item.get("omitted_fields", ())) | set(missing))
        counts = collection_counts(projected, original)
        item["row_counts"] = {path: {
            "returned_rows": 1 if path == "/" else counts.get(path, {}).get("returned_rows", 0),
            "total_rows": count["total_rows"],
        } for path, count in item.get("row_counts", {}).items()}

    # Full profiles can be large. Keep every completed account's compact facts
    # before considering a smaller page. Missing fields are explicit, never an
    # arbitrary JSON excerpt passed off as a complete profile.
    for item in items:
        original = item.get("data")
        if item.get("status") == "ok" and isinstance(original, dict):
            projected = summary(live.get("kind"), original)
            if projected != original:
                project(item, projected, "summary")
    target["projection"] = "summary"
    encoded = encode()
    if len(encoded) <= limit:
        return encoded

    # Preserve the league/count facts of the entire cohort when even summaries
    # are too large. Do not lose a zero-trophy account's ranked league.
    if live.get("kind") in ("player", "clan"):
        keys = ("tag", "name", "townHallLevel", "role", "leagueTier", "league", "trophies", "bestTrophies") \
            if live["kind"] == "player" else (
                "tag", "name", "members", "memberList.tag", "memberList.townHallLevel",
            )
        for item in items:
            if isinstance(item.get("data"), dict):
                project(item, select_fields(item["data"], keys), "facts")
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
    target["omitted_fields"] = ["/items/*/data"]
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
    # Large batches can exhaust the allowance with per-account collection
    # metadata alone. Still identify every read outcome and omitted tag.
    combined_counts = {}
    for item in totals:
        for path, count in item["collections"].items():
            combined = combined_counts.setdefault(path, {"returned_rows": 0, "total_rows": 0})
            combined["total_rows"] += count["total_rows"]
    total_rows = sum(count["total_rows"] for count in combined_counts.values())
    minimal = {
        "source": "clash_api", "kind": live.get("kind"), "truncated": True,
        "flags": target["flags"], "read_statuses": target["read_statuses"],
        "next_tags": [item["tag"] for item in items], "returned_rows": 0, "total_rows": total_rows,
        "omitted_fields": ["/items/*/data"], "omitted_collection_counts": combined_counts,
    }
    if live is not payload:
        minimal["omitted_fields"] = ["/" + key for key in payload]
    encoded = json.dumps(minimal, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= limit:
        return encoded
    encoded = json.dumps({"truncated": True, "returned_rows": 0,
                          "total_rows": total_rows},
                         separators=(",", ":"))
    return encoded if len(encoded) <= limit else ""



def error_result(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)
