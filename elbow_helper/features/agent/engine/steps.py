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
from ..access import require_access, accessible_message_channel, has_access_requirements
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
    if not has_access_requirements(context.guild, context.member.id, contract.required_access):
        return {"error": "This needs " + ", ".join(sorted(contract.required_access)) + " access.",
                "required_access": sorted(contract.required_access)}
    for channel_id in channels:
        if await accessible_message_channel(context, channel_id) is None:
            return {"error": "The asker cannot access that conversation."}

    return {}


@dataclass(slots=True)
class PlanExecutionState:
    plan: Mapping[str, Any]
    original: dict[str, Any]
    emitted: list[AgentToolResult]


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
        self.completed_steps = {}
        self.source_identities = dict(context.state.report_sources)
        for turn in context.state.authorized_history or context.history:
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
        self.model_results = {}
        self.sent_summaries = {}

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
            return {"error": checked.error, "offered": checked.offered}
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
            if self.tool_calls >= limits.MAX_TOOL_CALLS:
                return {"error": "The request reached its lookup limit."}
            if self.evidence_characters >= limits.MAX_EVIDENCE_CHARACTERS:
                return {"error": "The request reached its evidence limit."}
            timeout = (
                limits.COMMAND_TIMEOUT_SECONDS
                if tool.effect is AgentCapabilityEffect.COMMAND
                else limits.TOOL_TIMEOUT_SECONDS
            )
            if self.context.deadline_monotonic is not None and (
                time.monotonic()
                + timeout
                + limits.FINAL_ANSWER_TIME_RESERVE_SECONDS
                + limits.DELIVERY_TIME_RESERVE_SECONDS
                >= self.context.deadline_monotonic
            ):
                return {"error": "The request has no time for another lookup."}
            if not self.budget.can_continue_tools(
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
        local = replace(
            local,
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
            view = model_view(
                payload,
                summaries=summaries if payload.get("report_id") in local.state.reports else None,
                row_fields={field for field, value in payload.items() if isinstance(value, list)},
            )
            model_content = json.dumps(view, ensure_ascii=False, default=str, separators=(",", ":"))
            structured_content = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))
            limit = self.budget.result_character_limit(
                plan_state.emitted,
                pending_call_ids=(step["id"],),
                maximum=min(
                    limits.MAX_TOOL_RESULT_CHARACTERS,
                    limits.MAX_EVIDENCE_CHARACTERS - self.evidence_characters,
                ),
            )
            content = bound_tool_result(model_content, limit)
            self.model_results[step["id"]] = (
                json.loads(content) if content == model_content else
                model_result(json.loads(content) if content else {}, truncated=True)
            )
            if content == model_content:
                self.sent_summaries = summaries
            plan_state.emitted.append(AgentToolResult(step["id"], content))
            self.evidence_characters += len(content) if content else max(0, limit)
            merge_tool_state(self.context, local, previous)
            self.source_identities.update(local.state.report_sources)
            self._remember_sources(payload, scope, arguments)
            self.context.state.evidence.append(
                evidence_record(
                    call_id=step["id"],
                    tool=step["capability"],
                    arguments=arguments,
                    result=structured_content,
                    raw_result_characters=len(structured_content),
                    result_complete=content == model_content,
                    capability_scope=scope,
                    context=self.context,
                )
            )
        return payload

    async def run(self, plan: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
        self.model_results = {}
        original_summaries = {key: values.copy() for key, values in self.sent_summaries.items()}
        plan_state = PlanExecutionState(
            plan=plan,
            original=tool_state_snapshot(self.context),
            emitted=[],
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
                is AgentCapabilityEffect.READ,
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
            })
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
