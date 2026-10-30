"""Checked request steps and retained evidence."""

from __future__ import annotations

import logging

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from collections.abc import Mapping
import json
import time
from typing import Any
from elbow_helper.infrastructure.ai import AgentToolResult
from ..models import AgentCapabilityEffect, AgentRequestContext
from ..actions.contracts import ActionClass, PreparedAction
from ..wording import ACTION_UNAVAILABLE
from ..access import AgentAccessLost
from ..access import require_access, accessible_message_channel, has_lookup_access
from ..access import message_channel_unavailable
from ..access import require_evidence_access
from ..reports.tools import original_tool
from ..plan.checker import check_plan, check_step
from ..plan.executor import execute_plan
from ..plan.results import model_result, model_view
from . import budgets as limits
from functools import partial
from .rounds import ModelRounds
from .budgets import ContextBudget
from .capability_contract import CapabilityBindError, compile_capability_call
from ..models import RegisteredAgentTool
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


class PlanRunner:
    """Run checked steps and retain bounded evidence for the answer round."""

    def __init__(
        self,
        *,
        service: AgentService,
        context: AgentRequestContext,
        registry: Mapping[str, RegisteredAgentTool],
        command_capabilities: Mapping[str, CommandCapability],
        budget: ContextBudget,
        rounder: ModelRounds,
    ) -> None:
        self.service = service
        self.context = context
        self.registry = registry
        self.command_capabilities = command_capabilities
        self.budget = budget
        self.rounder = rounder
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
        self.model_results = {}
        self.sent_summaries = {}

    @property
    def evidence_limit(self):
        # Conversations carry many speakers and citation envelopes. Their
        # chunks still share the existing token, round and deadline budgets.
        return (limits.MAX_DISCUSSION_EVIDENCE_CHARACTERS if self.discussion_reads
                else limits.MAX_EVIDENCE_CHARACTERS)

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
            return checked
        key = step["capability"] + json.dumps(arguments, sort_keys=True, default=str)
        future = None
        if (
            checked["tool"].effect is AgentCapabilityEffect.READ
            and checked["tool"].action_class is ActionClass.READ
            # These handlers recheck freshness and project request-scoped raw reads.
            and step["capability"] not in ("read_clash", "read_ranked_stats", "read_current_war")
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
                result = reserved
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
                    "cause": "invalid_arguments", "argument_schema": self.registry[name].definition.parameters}
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
        if payload.get("cause") == "invalid_arguments":
            payload.update(capability=step["capability"], argument_schema=tool.definition.parameters)
        if tool.effect is AgentCapabilityEffect.ARTIFACT and "error" not in payload:
            # Retained legacy title provenance remains part of artifact access.
            local.state.required_access.update(local.state.knowledge_title_access)
        payload = model_result(payload)
        return await self._record_result(
            step, arguments, checked["scope"], local, previous, payload, checked["contract"], plan_state,
        )

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
            summaries = {key: values.copy() for key, values in self.sent_summaries.items()}
            structured_content = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))
            limit = self.budget.result_character_limit(
                plan_state.emitted,
                pending_call_ids=(step["id"],),
                maximum=min(
                    limits.MAX_TOOL_RESULT_CHARACTERS,
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
            merge_tool_state(self.context, local, previous)
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
        try:
            result = await execute_plan(
                plan,
                partial(self.run_one, plan_state=plan_state),
                max_concurrency=4,
                earlier_results=self.results,
                argument_schema=lambda step: (
                    self.registry[step["capability"]].definition.parameters
                ),
                step_errors=check_plan(
                    plan, self.registry, completed_steps=self.completed_steps,
                ).step_errors,
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
