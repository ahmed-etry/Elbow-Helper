"""Checked request steps and retained evidence."""

from __future__ import annotations

import logging

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from collections.abc import Mapping
import json
import time
from typing import Any
from elbow_helper.infrastructure.ai import AgentToolResult, TextGenerationError
from elbow_helper.agent.engine.models import AgentCapabilityEffect, AgentRequestContext
from ..actions.contracts import ActionClass, PreparedAction
from elbow_helper.agent.conversation.wording import ACTION_UNAVAILABLE
from elbow_helper.agent.safety.access import AgentAccessLost
from elbow_helper.agent.safety.access import require_access, accessible_message_channel, has_lookup_access
from elbow_helper.agent.safety.access import message_channel_unavailable
from elbow_helper.agent.safety.access import require_evidence_access
from ..reports.tools import original_tool
from ..plan.checker import check_plan, check_step
from ..plan.executor import execute_plan, referenced_report_ids
from ..plan.input_rules import without_rules
from ..plan.results import model_result, model_view
from . import budgets as limits
from .lookup_health import lookup_problem
from .evidence_delta import conversation_notes, conversation_notes_prompt
from ..reads.snapshots import FamilyReport
from .rounds import AgentGracefulEnd
from functools import partial
from .rounds import ModelRounds
from .budgets import ContextBudget
from .capability_contract import CapabilityBindError, compile_capability_call
from elbow_helper.agent.engine.models import RegisteredAgentTool
from ..discord_actions.gif_ownership import consume_gifs
from ..commands.registry import CommandCapability
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .service import AgentService
from .tool_call import (
    clone_tool_state,
    tool_state_snapshot,
    merge_tool_state,
    bound_tool_result,
    evidence_record,
    discard_unpublished_results,
    restore_tool_state,
)

LOGGER = logging.getLogger(__name__)


async def disclosure_issue(
    context: AgentRequestContext, registry: Mapping[str, Any], step: Mapping[str, Any]
) -> dict[str, Any]:
    selected = original_tool(registry, step["capability"], step["arguments"])
    if (selected or registry[step["capability"]]).action_class in (
        ActionClass.CHANGE, ActionClass.IRREVERSIBLE,
    ):
        return {}
    contract = (
        selected.contract if selected else registry[step["capability"]].contract
    )
    if contract is None:
        return {}
    channels: set[int] = set()
    for field in contract.channel_fields:
        value = step["arguments"].get(field)
        if type(value) is int:
            channels.add(value)
        elif isinstance(value, list):
            channels.update(item for item in value if type(item) is int)
    await require_evidence_access(context)
    if not has_lookup_access(context, contract.required_access):
        return {"error": "This needs " + ", ".join(sorted(contract.required_access)) + " access.",
                "required_access": sorted(contract.required_access), "cause": "access_denied"}
    for channel_id in channels:
        if await accessible_message_channel(context, channel_id) is None:
            result = message_channel_unavailable(context, channel_id)
            result.setdefault("cause", "access_denied" if str(channel_id) in context.state.scope_audit.excluded
                              else "unavailable")
            result.setdefault("available_privately", False)
            return result

    return {}


@dataclass(slots=True)
class PlanExecutionState:
    plan: Mapping[str, Any]
    original: dict[str, Any]
    emitted: list[AgentToolResult]
    answer_complete: bool = False
    finished: set[str] = dataclass_field(default_factory=set)
    payloads: dict[str, Mapping[str, Any]] = dataclass_field(default_factory=dict)
    note_results: dict[str, AgentToolResult] = dataclass_field(default_factory=dict)


class PlanRunner:
    """Run checked steps and retain bounded evidence for the answer round."""

    def planning_state(self): return {"step_id_scope": "request", "used_step_ids": list(self.completed_steps)}

    def __init__(
        self,
        *,
        service: AgentService,
        context: AgentRequestContext,
        registry: Mapping[str, RegisteredAgentTool],
        command_capabilities: Mapping[str, CommandCapability],
        budget: ContextBudget,
        rounder: ModelRounds,
        session_factory=None,
    ) -> None:
        self.service = service
        self.context = context
        self.registry = registry
        self.command_capabilities = command_capabilities
        self.budget = budget
        self.rounder = rounder
        self.session_factory = session_factory
        self.state_lock = asyncio.Lock()
        self.reads = {}
        self.results = {}
        # Conversation retention can evict a report while this request still
        # needs its complete rows. These snapshots live only with this runner.
        self.report_snapshots = {}
        self.completed_steps = {}
        self.source_identities = dict(context.state.report_sources)
        for turn in (context.history if context.state.authorized_history is None
                     else context.state.authorized_history):
            if turn.record is None:
                continue
            for encoded in turn.record.evidence:
                try:
                    record = json.loads(encoded)
                    self._remember_sources(
                        json.loads(record["result"]), record.get("capability_scope", {}),
                        record["arguments"],
                    )
                except (KeyError, TypeError, ValueError):
                    continue
        self.tool_calls = 0
        self.evidence_characters = 0
        self.discussion_reads = False
        self.data_query_reads = False
        self.model_results = {}
        self.sent_summaries = {}

    @property
    def evidence_limit(self):
        # Conversations carry many speakers and citation envelopes. Their
        # chunks still share the existing token, round and deadline budgets.
        limit = (limits.MAX_DISCUSSION_EVIDENCE_CHARACTERS if self.discussion_reads
                 else limits.MAX_EVIDENCE_CHARACTERS)
        # The schema guide and a whole-family SQL summary share this room.
        return max(limit, limits.MAX_QUERY_EVIDENCE_CHARACTERS) if self.data_query_reads else limit

    def _remember_sources(self, payload, scope, arguments):
        channels = set(scope.get("bound_source_channels", ()))
        for field in scope.get("channel_fields", ()):
            value = arguments.get(field)
            channels.update(
                item for item in (value if isinstance(value, list) else [value])
                if type(item) is int
            )
        fields = {field for tool in self.registry.values() if tool.contract
                  for field in tool.contract.retained_fields}
        def visit(value):
            if isinstance(value, Mapping):
                for key, item in value.items():
                    if key in fields:
                        for identity in item if isinstance(item, list) else [item]:
                            if isinstance(identity, str) and channels:
                                self.source_identities.setdefault(identity, frozenset(channels))
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
        visit(payload)

    async def run_one(
        self, step, arguments, earlier_results, *, plan_state,
    ):
        started = time.monotonic()
        result = None
        status = "failed"
        try:
            result = await self._run_one(step, arguments, earlier_results, plan_state=plan_state)
            status = "completed"
            plan_state.finished.add(step["id"])
            return result
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            debug = getattr(self.context, "operator_debug", None)
            if debug is not None:
                debug.tool_calls.append({
                    "step_id": step["id"], "capability": step["capability"],
                    "arguments": deepcopy(arguments), "result": deepcopy(result),
                    "status": status, "elapsed_ms": int((time.monotonic() - started) * 1000),
                })

    async def _run_one(
        self,
        step: Mapping[str, Any],
        arguments: Mapping[str, Any],
        earlier_results: Mapping[str, Any],
        *,
        plan_state: PlanExecutionState,
    ) -> Mapping[str, Any]:
        checked = await self._check_step(step, arguments, earlier_results, plan_state)
        if "error" in checked:
            LOGGER.warning("Agent planned step refused: step=%s capability=%s error=%s",
                           step["id"], step["capability"], checked["error"])
            return {**checked, "execution_status": "not_run"}
        key = step["capability"] + json.dumps(arguments, sort_keys=True, default=str)
        future = None
        if (
            checked["tool"].effect is AgentCapabilityEffect.READ
            and checked["tool"].action_class is ActionClass.READ
            # These handlers recheck freshness and project request-scoped raw reads.
            and step["capability"] not in ("read_clash", "read_current_war")
            and not arguments.get('fresh')
            and 'fresh' not in checked['tool'].definition.parameters.get('properties', {})
        ):
            async with self.state_lock:
                if key in self.reads:
                    future = self.reads[key]
                    owner = False
                else:
                    future = self.reads[key] = asyncio.get_running_loop().create_future()
                    owner = True
            if not owner:
                await require_evidence_access(self.context)
                return await asyncio.shield(future)
        try:
            reserved = await self._reserve_tool(step, arguments, checked["tool"], plan_state)
            if isinstance(reserved, dict):
                result = {**reserved, "execution_status": "not_run"}
            else:
                local, previous = reserved
                result = await self._execute_checked(
                    step, arguments, checked, local, previous, plan_state,
                )
            if future is not None:
                future.set_result(result)
                # Coalesce in-flight reads, but a failed transport/setup/access
                # result must not replace the next real retry. Invalid arguments
                # are bounded by the flow using their explicit cause.
                if "error" in result or result.get("flags", {}).get("status") == "failed":
                    self.reads.pop(key, None)
            return result
        except BaseException:
            if future is not None:
                future.cancel()
                self.reads.pop(key, None)
            raise

    async def _check_step(self, step, arguments, earlier_results, plan_state: PlanExecutionState):
        name = step["capability"]
        checked = check_step({**step, "arguments": arguments}, self.registry, resolved=True)
        if not checked.ok:
            return {"error": checked.error, "offered": checked.offered,
                    "cause": "invalid_arguments",
                    **({"invalid_fields": list(checked.fields)} if checked.fields else {}),
                    **({"missing_fields": list(checked.missing_fields)} if checked.missing_fields else {}),
                    "argument_schema": without_rules(self.registry[name].definition.parameters)}
        try:
            scope = compile_capability_call(
                checked.tool, arguments, self.source_identities, contract=checked.contract,
            )
        except CapabilityBindError as error:
            return {"error": str(error)}
        issue = await disclosure_issue(
            self.context, self.registry, {**step, "arguments": arguments}
        )
        if issue:
            return issue
        return {"name": name, "tool": checked.tool, "contract": checked.contract, "scope": scope}

    async def _reserve_tool(self, step, arguments, tool, plan_state: PlanExecutionState):
        async with self.state_lock:
            if step['capability'] in (
                'read_conversation_evidence', 'read_discord_channel_history', 'read_conversation_history',
            ):
                self.discussion_reads = True
                if self.budget.request_limits == limits.FAMILY_LIMITS:
                    self.budget.request_limits = limits.RESEARCH_LIMITS
            if self.tool_calls >= limits.MAX_TOOL_CALLS:
                return {"error": "The request reached its lookup limit."}
            if self.evidence_characters >= self.evidence_limit:
                return {"error": "The request reached its evidence limit."}
            timeout = (
                limits.COMMAND_TIMEOUT_SECONDS
                if tool.effect is AgentCapabilityEffect.COMMAND
                else limits.TOOL_TIMEOUT_SECONDS
            )
            if self.context.deadline_monotonic is not None and (
                time.monotonic()
                + timeout
                + (0 if plan_state.answer_complete else limits.FINAL_ANSWER_TIME_RESERVE_SECONDS)
                + limits.DELIVERY_TIME_RESERVE_SECONDS
                >= self.context.deadline_monotonic
            ):
                return {"error": "The request has no time for another lookup."}
            # A finished assessment needs no further provider input/output or
            # composition reserve. Preparation still shares tool/evidence caps,
            # the hard deadline, and every normal action/access check.
            if not plan_state.answer_complete and not self.budget.can_continue_tools(
                self.budget.projected_input(plan_state.emitted),
                result_reserve=8192,
            ):
                return {"error": "The request has no context room for another lookup."}
            self.tool_calls += 1
            local = replace(self.context, state=clone_tool_state(self.context.state))
            previous = tool_state_snapshot(local)
        return local, previous

    async def _execute_checked(
        self, step, arguments, checked, local, previous, plan_state: PlanExecutionState
    ):
        search_pages = 0

        async def reserve_search_page(characters):
            nonlocal search_pages
            async with self.state_lock:
                remaining = self.evidence_limit - self.evidence_characters - characters
                if remaining <= 0:
                    return 0
                if search_pages:
                    if self.tool_calls >= limits.MAX_TOOL_CALLS:
                        return 0
                    if local.deadline_monotonic is not None and (
                        time.monotonic() + limits.TOOL_TIMEOUT_SECONDS
                        + (0 if plan_state.answer_complete else limits.FINAL_ANSWER_TIME_RESERVE_SECONDS)
                        + limits.DELIVERY_TIME_RESERVE_SECONDS >= local.deadline_monotonic
                    ):
                        return 0
                    if not plan_state.answer_complete and not self.budget.can_continue_tools(
                        self.budget.projected_input(plan_state.emitted), result_reserve=8192,
                    ):
                        return 0
                    self.tool_calls += 1
                search_pages += 1
                return remaining

        local = replace(
            local,
            reserve_search_page=reserve_search_page,
            member=require_access(
                local.guild,
                local.member.id,
                local.source_message.channel,
            ),
        )
        await require_evidence_access(local)
        tool = checked["tool"]
        checked_arguments = deepcopy(arguments)
        raw = await self.service.execute_tool(
            name=checked["name"],
            handler=tool.handler,
            arguments=arguments,
            capability_scope=checked["scope"],
            context=local,
            action_class=tool.action_class,
            timeout_seconds=(
                limits.COMMAND_TIMEOUT_SECONDS
                if tool.effect is AgentCapabilityEffect.COMMAND
                else limits.TOOL_TIMEOUT_SECONDS
            ),
        )
        for index in range(len(previous["proposed_changes"]), len(local.state.proposed_changes)):
            proposal = local.state.proposed_changes[index]
            if isinstance(proposal, PreparedAction):
                if proposal.action_class is not tool.action_class:
                    LOGGER.error(
                        "Agent action class mismatch: capability=%s step=%s "
                        "registered=%s prepared=%s",
                        step["capability"], step["id"], tool.action_class, proposal.action_class,
                    )
                    restore_tool_state(local, previous)
                    raw = json.dumps({"error": ACTION_UNAVAILABLE})
                    break
                local.state.proposed_changes[index] = replace(
                    proposal,
                    step_id=step["id"],
                    capability_name=step["capability"],
                    checked_arguments=deepcopy(checked_arguments),
                )
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"error": "Invalid lookup result"}
        raw = None
        if (self.session_factory is not None and checked['name'] == 'read_conversation_evidence'
                and payload.get('mode') == 'channel_window' and payload.get('source_next_cursor')
                and (payload.get('coverage', {}).get('requested_after')
                     or payload.get('coverage', {}).get('requested_before'))
                and arguments.get('delivery', 'text') == 'text'
                and arguments.get('view', 'rows') == 'rows'
                and not arguments.get('report_id') and not arguments.get('cursor')
                and not arguments.get('offset')):
            payload = await self._conversation_period(step, payload, arguments, checked, local, previous, plan_state)
        if payload.get("cause") == "invalid_arguments":
            payload.update(capability=step["capability"], argument_schema=without_rules(tool.definition.parameters))
        if tool.effect is AgentCapabilityEffect.ARTIFACT and "error" not in payload:
            # Retained legacy title provenance remains part of artifact access.
            local.state.required_access.update(local.state.knowledge_title_access)
        payload = model_result(payload)
        return await self._record_result(
            step, arguments, checked["scope"], local, previous, payload, checked["contract"], plan_state,
        )

    async def _conversation_period(self, step, payload, arguments, checked, local, previous, plan_state):
        """Finish a long window in bounded reads, retaining only grounded notes.

        The first read is unchanged. Only a continuation starts a side session.
        Subsequent complete chunks share one lookup reservation each; their
        internal Discord pages do not become separate planning operations.
        """
        first_id = payload['report_id']
        template = {key: value for key, value in payload.items() if key not in (
            'rows', 'flags', 'coverage', 'source_next_cursor', 'returned_rows',
            'total_rows', 'count', 'next_offset', 'next_cursor', 'offset')}
        coverage = {**payload['coverage'], **{key: 0 for key in (
            'scanned_messages_in_window', 'returned_messages', 'scanned_count',
            'matched_count', 'pages_scanned')}, 'parts': 0}
        coverage['next_cursor'] = arguments.get('source_cursor')
        coverage['reached_requested_start'] = False
        coverage['unread_period'] = {'after': coverage['requested_after'],
                                    'before': coverage['requested_before']}
        notes, sources, batch, chunks = [], {}, [], []

        def notes_report(part_notes, part_sources, part_coverage):
            summary = {**template, 'coverage': part_coverage,
                'source_next_cursor': part_coverage['next_cursor'], 'notes': part_notes,
                'message_sources': list(part_sources.values()), 'message_content_complete': False,
                'count': part_coverage['returned_messages']}
            summary.pop('report_id', None)
            return FamilyReport(first_id, local.guild.id, 'read_conversation_evidence',
                json.dumps({'rows': [], 'summary': summary}, ensure_ascii=False))

        async def reduce_batch():
            if not batch:
                return True
            await require_evidence_access(local)
            try:
                reduced, citations = await conversation_notes(self, batch)
            except (AgentGracefulEnd, TextGenerationError, TimeoutError):
                return False
            await require_evidence_access(local)
            # Do not replace an evidence overflow with trimmed notes. Leave
            # this entire part unread, with the last completed boundary.
            next_coverage = dict(coverage)
            for chunk in chunks:
                for field in ('scanned_messages_in_window', 'returned_messages', 'scanned_count',
                              'matched_count', 'pages_scanned'):
                    next_coverage[field] += chunk.get(field, 0)
                for field in ('next_cursor', 'reached_requested_start', 'stopped_at_message_budget'):
                    next_coverage[field] = chunk[field]
                if 'unread_period' in chunk:
                    next_coverage['unread_period'] = chunk['unread_period']
            next_coverage['parts'] += 1
            if next_coverage['reached_requested_start']:
                next_coverage.pop('unread_period', None)
            next_sources = {**sources, **{row['message_id']: row for row in citations}}
            retained = json.dumps(model_view(model_result(notes_report(
                notes + reduced, next_sources, next_coverage).page())),
                ensure_ascii=False, separators=(',', ':'))
            from ..research.history import discussion_result_characters
            async with self.state_lock:
                reservation = plan_state.note_results.get(step['id'])
                previous_size = len(reservation.content) if reservation else 0
                other_notes = [value for identity, value in plan_state.note_results.items()
                               if identity != step['id']]
                limit = self.budget.result_character_limit([*plan_state.emitted, *other_notes],
                    pending_call_ids=tuple(item['id'] for item in plan_state.plan['steps']),
                    maximum=min(discussion_result_characters(),
                        self.evidence_limit - self.evidence_characters + previous_size))
                if len(retained) > limit:
                    return False
                self.evidence_characters += len(retained) - previous_size
                plan_state.note_results[step['id']] = AgentToolResult(step['id'], retained)
            notes.extend(reduced)
            sources.update(next_sources)
            coverage.clear()
            coverage.update(next_coverage)
            batch.clear()
            chunks.clear()
            return True

        try:
            while True:
                if 'error' in payload:
                    if payload.get('cause') == 'access_denied' or payload.get('required_access'):
                        return payload
                    await require_evidence_access(local)
                    await reduce_batch()
                    break
                identifier = payload['report_id']
                # Family paging is transport, not source coverage: review all
                # rows in the authorized chunk, including rows beyond its page.
                rows = json.loads(local.state.reports[identifier].payload)['rows']
                # The pending batch owns these complete rows until its notes
                # exist; temporary reports need not compete for retention.
                for field in ('reports', 'report_sources', 'report_access_requirements'):
                    getattr(local.state, field).pop(identifier, None)
                payload.pop('rows', None)
                prompt_size = len(conversation_notes_prompt(self.context.state.request_text, batch + rows))
                if batch and prompt_size > limits.MAX_DISCUSSION_EVIDENCE_CHARACTERS:
                    if not await reduce_batch():
                        break
                batch.extend(rows)
                chunks.append(payload['coverage'])
                cursor = payload['source_next_cursor']
                if not cursor:
                    await reduce_batch()
                    break
                async with self.state_lock:
                    can_read = (self.tool_calls < limits.MAX_TOOL_CALLS and (
                        local.deadline_monotonic is None or time.monotonic()
                        + limits.TOOL_TIMEOUT_SECONDS + limits.MODEL_ROUND_TIME_RESERVE_SECONDS
                        + limits.FINAL_ANSWER_TIME_RESERVE_SECONDS
                        + limits.DELIVERY_TIME_RESERVE_SECONDS < local.deadline_monotonic))
                    if can_read:
                        self.tool_calls += 1
                if not can_read:
                    await reduce_batch()
                    break

                async def reserve_chunk(characters):
                    return limits.MAX_DISCUSSION_EVIDENCE_CHARACTERS - characters

                continuation = {key: value for key, value in arguments.items()
                                if key not in ('source_cursor', 'limit')}
                continuation['source_cursor'] = cursor
                raw = await self.service.execute_tool(name=checked['name'], handler=checked['tool'].handler,
                    arguments=continuation, capability_scope=checked['scope'],
                    context=replace(local, reserve_search_page=reserve_chunk),
                    action_class=checked['tool'].action_class, timeout_seconds=limits.TOOL_TIMEOUT_SECONDS)
                payload = json.loads(raw)
                raw = None
            await require_evidence_access(local)
        except BaseException:
            async with self.state_lock:
                reservation = plan_state.note_results.pop(step['id'], None)
                if reservation:
                    self.evidence_characters -= len(reservation.content)
            raise
        finally:
            # Nothing containing a body is published to the request or kept in
            # report snapshots, even when a side call times out or access ends.
            batch.clear()
            for identifier in set(local.state.reports) - set(previous['reports']):
                for field in ('reports', 'report_sources', 'report_access_requirements'):
                    getattr(local.state, field).pop(identifier, None)
        report = notes_report(notes, sources, coverage)
        local.state.reports[first_id] = report
        local.state.report_sources[first_id] = frozenset(local.state.source_channels)
        local.state.report_access_requirements[first_id] = frozenset(local.state.required_access)
        return report.page()

    def _reports_still_needed(self, current, plan_state: PlanExecutionState) -> set[str]:
        """Reports that steps of this plan other than `current` have yet to use."""
        known = {**self.results, **plan_state.payloads}
        retained = set(self.context.state.reports)
        retained.update(report_id for report_id in (
            payload.get("report_id") for payload in plan_state.payloads.values())
            if isinstance(report_id, str))
        needed: set[str] = set()
        for step in plan_state.plan["steps"]:
            if step["id"] != current["id"] and step["id"] not in plan_state.finished:
                needed |= referenced_report_ids(step["arguments"], known, retained)
        return needed

    async def _record_result(
        self,
        step,
        arguments,
        scope,
        local,
        previous,
        payload,
        contract,
        plan_state: PlanExecutionState,
    ):
        async with self.state_lock:
            reservation = plan_state.note_results.pop(step['id'], None)
            if reservation:
                self.evidence_characters -= len(reservation.content)
            query_data = (step['capability'] == 'query_bot_data'
                          or step['capability'] == 'open_capability_group' and 'data_guide' in payload)
            self.data_query_reads |= query_data
            summaries = {key: values.copy() for key, values in self.sent_summaries.items()}
            structured_content = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))
            from elbow_helper.agent.research.history import discussion_result_characters
            limit = self.budget.result_character_limit(
                [*plan_state.emitted, *plan_state.note_results.values()],
                pending_call_ids=(step["id"],),
                maximum=min(
                    (limits.MAX_QUERY_RESULT_CHARACTERS if query_data
                     else discussion_result_characters() if (
                         payload.get('family') == 'read_conversation_evidence'
                         and payload.get('mode') == 'channel_window')
                     else limits.MAX_TOOL_RESULT_CHARACTERS),
                    self.evidence_limit - self.evidence_characters,
                ),
            )
            # Bound a complete family envelope before suppressing its repeated
            # summary. Continuations must address the rows actually delivered.
            delivery = payload
            page_bounded = False
            if payload.get('kind') == 'family':
                complete = json.dumps(model_view(payload), ensure_ascii=False,
                                      default=str, separators=(',', ':'))
                bounded_page = bound_tool_result(complete, limit)
                page_bounded = bounded_page != complete
                delivery = model_result(json.loads(bounded_page or '{}'))
            view = model_view(
                delivery,
                summaries=summaries if delivery.get("report_id") in local.state.reports else None,
                row_fields={field for field, value in payload.items() if isinstance(value, list)},
            )
            model_content = json.dumps(view, ensure_ascii=False, default=str, separators=(",", ":"))
            content = bound_tool_result(model_content, limit)
            bounded = json.loads(content) if content else {}
            family_page = (bounded.get('kind') == 'family' and bounded.get('report_id') == payload.get('report_id')
                           and not bounded.get('truncated') and 'error' not in bounded)
            self.model_results[step["id"]] = (
                json.loads(content) if content == model_content else
                model_result(bounded, truncated=not family_page)
            )
            if content == model_content:
                self.sent_summaries = summaries
            plan_state.emitted.append(AgentToolResult(step["id"], content))
            self.evidence_characters += len(content) if content else max(0, limit)
            identifier = payload.get('report_id')
            if identifier in local.state.reports:
                self.report_snapshots[identifier] = (
                    local.state.reports[identifier],
                    local.state.report_sources.get(identifier, frozenset()),
                    local.state.report_access_requirements.get(identifier, frozenset()),
                )
            plan_state.payloads[step["id"]] = payload
            merge_tool_state(
                self.context, local, previous, self._reports_still_needed(step, plan_state),
            )
            self.source_identities.update(local.state.report_sources)
            self._remember_sources(payload, scope, arguments)
            self.context.state.evidence.append(
                evidence_record(
                    call_id=step["id"],
                    tool=step["capability"],
                    arguments=arguments,
                    result=(content if payload.get("source") == "clash_api"
                            and content != model_content else structured_content),
                    model_result_content=content,
                    raw_result_characters=len(structured_content),
                    result_complete=content == model_content and not page_bounded,
                    capability_scope=scope,
                    context=self.context,
                    plan=plan_state.plan if len(plan_state.emitted) == 1 else None,
                )
            )
        return payload

    async def run(self, plan: Mapping[str, Any], *, answer_complete=False) -> dict[str, Mapping[str, Any]]:
        if answer_complete and any(self.registry[step['capability']].action_class not in (
                ActionClass.CHANGE, ActionClass.IRREVERSIBLE) for step in plan['steps']):
            raise ValueError('Only change previews may follow a completed answer')
        self.model_results = {}
        original_summaries = {key: values.copy() for key, values in self.sent_summaries.items()}
        plan_state = PlanExecutionState(
            plan=plan,
            original=tool_state_snapshot(self.context),
            emitted=[],
            answer_complete=answer_complete,
        )
        checked = check_plan(plan, self.registry, completed_steps=self.completed_steps)
        try:
            result = await execute_plan(
                plan,
                partial(self.run_one, plan_state=plan_state),
                max_concurrency=4,
                earlier_results=self.results,
                argument_schema=lambda step: without_rules(
                    self.registry[step["capability"]].definition.parameters
                ),
                step_errors=checked.step_errors,
                step_fields=checked.step_fields,
                step_missing_fields=checked.step_missing_fields,
                parallel=lambda step: self.registry[step["capability"]].effect
                is AgentCapabilityEffect.READ and self.registry[step["capability"]].action_class
                is ActionClass.READ,
            )
            result = {
                step_id: value if "flags" in value else model_result(value)
                for step_id, value in result.items()
            }
            await require_evidence_access(self.context)
            self.results.update(result)
            self.context.state.feedback_lookup_problem |= lookup_problem(result)
            self.completed_steps.update({
                step["id"]: step for step in plan["steps"]
                if "error" not in result[step["id"]]
                and result[step["id"]].get("flags", {}).get("status") != "failed"
                and not (result[step["id"]].get("source") == "clash_api"
                         and not any(item.get("status") == "ok"
                                     for item in result[step["id"]].get("items", ())))
            })
            consume_gifs(self.context.state, self.completed_steps, self.results,
                         allow_repeats=self.rounder.social_route)
            self.rounder.unpublished = plan_state.original
            return result
        except AgentAccessLost:
            self.model_results = {}
            self.sent_summaries = original_summaries
            await discard_unpublished_results(self.context, plan_state.original, ())
            return {
                step["id"]: model_result({"error": "That lookup failed."}) for step in plan["steps"]
            }

    def model_results_for(self, results):
        return {step_id: self.model_results[step_id] if step_id in self.model_results else model_view(payload)
                for step_id, payload in results.items()}
