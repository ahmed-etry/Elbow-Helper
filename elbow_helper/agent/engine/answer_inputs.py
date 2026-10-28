"""Project read results into writer evidence without planner instructions.

Only structured transport/report envelopes are changed. Source prose, messages,
command notes and domain records are never classified or edited by text matching.
Raw evidence remains available for authorization, citation checks and retention.
"""

from copy import deepcopy
from datetime import datetime
import json

from .evidence_times import readable_evidence_times
from ..conversation.context import estimate_tokens
from .tool_call import bound_tool_result
from .member_facts import reader_roles, reference_facts
from .writer_tables import writer_tables


def serialized_answer_evidence(evidence):
    return json.dumps({'results': writer_tables(evidence)}, ensure_ascii=False, default=str,
                      separators=(',', ':'))


def bounded_answer_evidence(evidence, token_room, character_limit=64_000):
    """Size compact JSON with the same token estimate as the final request.

    A token allowance is not a character allowance divided by four. That old
    conversion discarded complete cohort rows even with ample context room.
    Generic bounding remains available when the serialized facts really exceed
    the remaining input allowance or the evidence character ceiling.
    """
    content = serialized_answer_evidence(evidence)
    if character_limit is None:
        # Complete typed tables use the actual remaining token allowance.
        # A separate character ceiling must not silently remove identities
        # that fit the already bounded provider/request input budget.
        character_limit = len(content)
    bounded = bound_tool_result(content, character_limit)
    while estimate_tokens(bounded) > token_room:
        if character_limit <= 2:
            return '{}'
        character_limit = max(2, min(character_limit - 1,
            int(len(bounded) * token_room / estimate_tokens(bounded) * 0.95)))
        bounded = bound_tool_result(content, character_limit)
    return bounded


# These fields describe how to obtain/present evidence, not domain facts.
_NAVIGATION = frozenset({
    "report_id", "family", "schema_version", "requester_id", "provenance", "source_next_cursor", "source_versions",
    "selection", "resolved_inputs", "offset", "next_offset", "next_cursor",
    "returned_rows", "complete_snapshot", "flags", "argument_schema",
    "capability", "detail_projection", "collection_limits", "coverage_note",
    "member_limits", "store_status", "reused_result", "reused_sections", "instruction",
})
_COVERAGE_MECHANICS = frozenset({
    "message", "reasons", "offset",
    "next_offset", "next_cursor",
})
_SUBJECT_FIELDS = frozenset({
    "member_id", "member_ids", "account_tags", "player_tag", "player_tags",
    "clan_code", "clan_codes", "clan_tags", "channel_id", "message_id", "role_id",
    "reference_kind", "stage", "subject", "target", "target_id",
})


def _coverage(value):
    if not isinstance(value, dict):
        return value
    projected = {key: deepcopy(item) for key, item in value.items()
                 if key not in _COVERAGE_MECHANICS}
    # Actual observation boundaries are facts. Unparsed locator descriptions
    # are still transport hints, not evidence of the period that was observed.
    for key in ('requested_after', 'requested_before'):
        item = projected.get(key)
        if item is None:
            continue
        try:
            datetime.fromisoformat(item)
        except (ValueError, TypeError):
            projected.pop(key, None)
    return projected


def read_handles(evidence):
    """Keep opaque paging handles separate from factual evidence when replanning."""
    handles = {}
    latest_sources = {}
    for identity, result in evidence.items():
        if not isinstance(result, dict) or result.get("error"):
            continue
        handle = {key: deepcopy(result[key]) for key in ("report_id", "next_offset", "next_cursor")
                  if result.get(key) is not None}
        coverage = result.get("coverage")
        source_cursor = result.get("source_next_cursor")
        if isinstance(coverage, dict) and "reached_requested_start" in coverage:
            source_cursor = source_cursor or coverage.get("next_cursor")
            scope = (result.get("channel_id"), result.get("mode"), result.get('stage'),
                     coverage.get("requested_after"), coverage.get("requested_before"))
            if scope[0]:
                previous = latest_sources.get(scope)
                if previous in handles:
                    handles[previous].pop("source_next_arguments", None)
                latest_sources[scope] = identity
        if source_cursor and result.get("channel_id"):
            if result.get('family') == 'read_recruitment_pipeline':
                selection = result.get('selection', {})
                handle['source_next_arguments'] = {
                    **{key: deepcopy(selection[key]) for key in
                       ('stage', 'after', 'before', 'member_ids', 'ticket_channel_id') if key in selection},
                    'stage': result.get('stage', selection.get('stage')),
                    'source_cursor': source_cursor}
            else:
                family = result.get("family") == "read_conversation_evidence"
                handle["source_next_arguments"] = {
                    "channel_id": result["channel_id"],
                    "source_cursor" if family else "cursor": source_cursor,
                    **({"mode": result.get("mode", "channel_window")} if family else {}),
                }
        if handle:
            handles[identity] = handle
    return handles


def _limit(result, flags, subject, guild):
    """Use typed failure metadata, never classify or rewrite error prose."""
    cause = result.get("cause")
    denied = (bool(result.get("required_access")) or cause == "access_denied"
              or "access_denied" in flags.get("limits", ()))
    needs_input = (result.get("status") == "needs_input" or bool(result.get("missing_fields"))
                   or cause == "needs_input")
    # Successful clarification reads can contain authorized candidate members,
    # option descriptions or a validation issue. Keep that data whole.
    limit = ({key: deepcopy(value) for key, value in result.items()
              if key not in _NAVIGATION and key not in ("error", "cause")}
             if needs_input and not denied and not result.get("error") else {})
    limit["status"] = "denied" if denied else "needs_input" if needs_input else "unavailable"
    for key in ("available_privately", "missing_fields", "missing", "subject",
                "role_change_access", "change_attempted", "confirmation_prepared"):
        if key in result:
            limit[key] = deepcopy(result[key])
    if "subject" not in limit:
        selected = {key: deepcopy(value) for key, value in subject.items() if key in _SUBJECT_FIELDS}
        selected.update({key: deepcopy(value) for key, value in result.items() if key in _SUBJECT_FIELDS})
        if selected:
            limit["subject"] = selected
    if result.get('required_access'):
        limit['reader_roles'] = reader_roles(guild, result['required_access'])
    return limit


def answer_evidence(evidence, *, subjects=None, guild=None):
    """Keep facts, actual coverage and verified limits; exclude read guidance."""
    projected = {}
    for identity, result in evidence.items():
        if not isinstance(result, dict):
            continue
        if identity == "retained":
            projected[identity] = answer_evidence(result, guild=guild)
            continue
        if "catalogue" in result and "total_capabilities" in result:
            continue
        flags = result.get("flags", {})
        if result.get("error") or result.get("status") == "needs_input" or (isinstance(flags, dict)
                                   and flags.get("status") in ("failed", "refused")):
            projected[identity] = _limit(result, flags if isinstance(flags, dict) else {},
                                        (subjects or {}).get(identity, {}), guild)
            continue
        if result.get('family') == 'read_community_reference':
            result = {**reference_facts(result, guild),
                      'family': result['family'], 'selection': result.get('selection', {})}
        facts = {key: deepcopy(value) for key, value in result.items() if key not in _NAVIGATION}
        if isinstance(facts.get('source_reads'), dict):
            facts['source_reads'] = answer_evidence(facts['source_reads'], guild=guild)
        # Keep provenance explicit without interpreting message or policy prose.
        # Background chat must not masquerade as a finding from the checked plan.
        if identity == "local_context":
            facts["evidence_role"] = "nearby_conversation"
        elif result.get("family") == "read_community_reference":
            facts["evidence_role"] = "reference"
            facts["reference_kind"] = result.get("selection", {}).get("reference_kind")
        if isinstance(flags, dict) and (flags.get("truncated") or flags.get("status") == "partial"):
            facts["evidence_complete"] = False
        for key in ("coverage", "history_coverage", "membership_coverage"):
            if key in facts:
                facts[key] = _coverage(facts[key])
        if result.get("source") == "clash_api" and isinstance(facts.get("items"), list):
            for item in facts["items"]:
                if isinstance(item, dict) and "coverage" in item:
                    item["coverage"] = _coverage(item["coverage"])
        if result.get("family") == "read_community_reference" and isinstance(facts.get("coverage"), str):
            del facts["coverage"]
        projected[identity] = facts
    return readable_evidence_times(projected)
