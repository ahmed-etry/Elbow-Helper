"""Checked request steps and retained evidence."""

from __future__ import annotations

import logging

import asyncio
from dataclasses import dataclass, replace
from collections.abc import Mapping
import json
import time
from typing import Any
from elbow_helper.infrastructure.ai import AgentToolResult
from ..models import AgentCapabilityEffect, AgentRequestContext
from ..actions.contracts import ActionClass, PreparedAction
from ..access import AgentAccessLost
from ..wording import ACTION_UNAVAILABLE
from ..access import require_access, accessible_message_channel, has_access_requirements
from ..access import require_evidence_access
from ..reports.tools import COMPARE_NAME, READ_NAME, original_tool
from ..plan.checker import entity_kind, parse_periods, check_step
from ..plan.executor import execute_plan, resolve_arguments
from ..plan.results import model_result
from ..plan.scope import resource_ids
from ..commands.bridge import check_command_plan
from . import budgets as limits
from functools import partial
from .rounds import ModelRounds
from ..plan.scope import ScopeLedger
from .budgets import ContextBudget
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
)

LOGGER = logging.getLogger(__name__)


async def disclosure_issue(
    context: AgentRequestContext, registry: Mapping[str, Any], step: Mapping[str, Any]
) -> str:
    selected = original_tool(registry, step["capability"], step["arguments"])
    if (selected or registry[step["capability"]]).action_class in (
        ActionClass.CHANGE, ActionClass.IRREVERSIBLE,
    ):
        return ""
    contract = (
        selected.contract if selected else registry[step["capability"]].contract
    )
    if contract is None:
        return ""
    channels: set[int] = set()
    for field in contract.channel_fields:
        value = step["arguments"].get(field)
        if type(value) is int:
            channels.add(value)
        elif isinstance(value, list):
            channels.update(item for item in value if type(item) is int)
    if not has_access_requirements(context.guild, context.member.id, contract.required_access):
        return ACTION_UNAVAILABLE
    for channel_id in channels:
        if await accessible_message_channel(context, channel_id) is None:
            return "The asker cannot access that conversation."

    return ""


@dataclass(slots=True)
class PlanExecutionState:
    plan: Mapping[str, Any]
    original: dict[str, Any]
    original_scope: dict[str, Any]
    emitted: list[AgentToolResult]
    periods: tuple[tuple[str, Any, Any], ...]
    entities: dict[str, set[str]]
    named: dict[str, set[str]]


class PlanRunner:
    """Run checked steps and retain bounded evidence for the answer round."""

    def __init__(
        self,
        *,
        service: AgentService,
        context: AgentRequestContext,
        registry: Mapping[str, RegisteredAgentTool],
        command_capabilities: Mapping[str, CommandCapability],
        sources: Mapping[str, frozenset[int | str]],
        budget: ContextBudget,
        ledger: ScopeLedger,
        rounder: ModelRounds,
    ) -> None:
        self.service = service
        self.context = context
        self.registry = registry
        self.command_capabilities = command_capabilities
        self.sources = sources
        self.budget = budget
        self.ledger = ledger
        self.rounder = rounder
        self.state_lock = asyncio.Lock()
        self.completed = set()
        self.tool_calls = 0
        self.evidence_characters = 0

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
            return checked
        reserved = await self._reserve_tool(step, arguments, checked["tool"], plan_state)
        if isinstance(reserved, dict):
            return reserved
        local, previous = reserved
        return await self._execute_checked(step, arguments, checked, local, previous, plan_state)

    async def _check_step(self, step, arguments, earlier_results, plan_state: PlanExecutionState):
        name = step["capability"]
        try:
            bound_periods = self._bound_periods(step, earlier_results, plan_state)
        except (KeyError, IndexError, TypeError):
            return {"error": "The declared period could not be resolved."}
        resolved_entities = self._resolved_entities(earlier_results, plan_state)
        retained = []

        def validate_scope(selected: RegisteredAgentTool) -> str:
            if self.registry[name].effect is AgentCapabilityEffect.COMMAND:
                issue = check_command_plan(
                    {**plan_state.plan, "steps": [{**step, "arguments": arguments}]},
                    self.command_capabilities,
                    self.sources,
                )
                if issue:
                    return issue
            contract = selected.contract
            if contract is None:
                return ""
            retained.extend(
                identity
                for field in contract.retained_fields
                if field in arguments
                for identity in (
                    arguments[field] if isinstance(arguments[field], list) else [arguments[field]]
                )
            )
            return self.ledger.check(retained, bound_periods, plan_state.named, resolved_entities)

        checked = check_step(
            {**step, "arguments": arguments},
            self.registry,
            bound_periods,
            resolved_entities,
            plan_state.named,
            set(step["depends_on"]),
            resolved=True,
            validate_scope=validate_scope,
        )
        if not checked.ok:
            return {"error": checked.error, "offered": checked.offered}
        scope = dict(checked.scope)
        if retained:
            scope["bound_source_channels"] = sorted(self.ledger.channels(retained))
        issue = await disclosure_issue(
            self.context, self.registry, {**step, "arguments": arguments}
        )
        if issue:
            return {"error": issue}
        return {"name": name, "tool": checked.tool, "contract": checked.contract, "scope": scope}

    def _bound_periods(self, step, earlier_results, plan_state: PlanExecutionState):
        bound = []
        for kind, value, extra in plan_state.periods:
            if kind != "resolved":
                bound.append((kind, value, extra))
                continue
            if value == step["id"]:
                continue
            key_value = earlier_results[value]
            for part in extra:
                key_value = key_value[part]
            for field, reference in step["arguments"].items():
                if isinstance(reference, dict) and reference == {"step": value, "path": extra}:
                    bound.append(("key", key_value, field))
        return tuple(bound)

    def _resolved_entities(self, earlier_results, plan_state: PlanExecutionState):
        resolved = {kind: set(values) for kind, values in plan_state.entities.items()}
        for entity in plan_state.plan["entities"]:
            if isinstance(entity["value"], dict):
                try:
                    value = resolve_arguments({"value": entity["value"]}, earlier_results)["value"]
                except (KeyError, IndexError, TypeError):
                    continue
                resolved.setdefault(entity_kind(entity["kind"]), set()).add(str(value))
        return resolved

    async def _reserve_tool(self, step, arguments, tool, plan_state: PlanExecutionState):
        key = step["capability"] + json.dumps(arguments, sort_keys=True, default=str)
        async with self.state_lock:
            if key in self.completed:
                return {"error": "This identical lookup already ran."}
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
            self.completed.add(key)
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
                local.state.proposed_changes[index] = replace(
                    proposal,
                    step_id=step["id"],
                )
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"error": "Invalid lookup result"}
        contract = checked["contract"]
        coverage_dates = {}
        if contract is not None and contract.time_window is not None:
            coverage_dates = {
                field: arguments[field] for field in contract.time_window[:2] if field in arguments
            }
        model_content = json.dumps(
            model_result(payload, coverage_dates=coverage_dates),
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
        return await self._record_result(
            step, arguments, checked["scope"], local, previous, payload, model_content, plan_state
        )

    async def _record_result(
        self,
        step,
        arguments,
        scope,
        local,
        previous,
        payload,
        model_content,
        plan_state: PlanExecutionState,
    ):
        async with self.state_lock:
            limit = self.budget.result_character_limit(
                plan_state.emitted,
                pending_call_ids=(step["id"],),
                maximum=min(
                    limits.MAX_TOOL_RESULT_CHARACTERS,
                    limits.MAX_EVIDENCE_CHARACTERS - self.evidence_characters,
                ),
            )
            content = bound_tool_result(model_content, limit)
            plan_state.emitted.append(AgentToolResult(step["id"], content))
            self.evidence_characters += len(content) if content else max(0, limit)
            merge_tool_state(self.context, local, previous)
            for identity in local.state.reports:
                if identity not in plan_state.original["reports"]:
                    self.ledger.remember(identity, step["capability"], arguments)
            for identity in resource_ids(payload, self.registry):
                self.ledger.remember(identity, step["capability"], arguments)
            self.context.state.evidence.append(
                evidence_record(
                    call_id=step["id"],
                    tool=step["capability"],
                    arguments=arguments,
                    result=content,
                    raw_result_characters=len(model_content),
                    result_complete=content == model_content,
                    capability_scope=scope,
                    context=self.context,
                )
            )
        try:
            return json.loads(content)
        except ValueError:
            return {"error": "The result could not be included."}

    async def run(self, plan: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
        entities: dict[str, set[str]] = {}
        for entity in plan["entities"]:
            if not isinstance(entity["value"], dict):
                entities.setdefault(entity_kind(entity["kind"]), set()).add(str(entity["value"]))
        plan_state = PlanExecutionState(
            plan=plan,
            original=tool_state_snapshot(self.context),
            original_scope=dict(self.ledger.reports),
            emitted=[],
            periods=parse_periods(plan["periods"]),
            entities=entities,
            named={
                entity_kind(kind): {str(value) for value in values}
                for kind, values in self.sources.items()
                if values
            },
        )
        try:
            result = await execute_plan(
                plan,
                partial(self.run_one, plan_state=plan_state),
                max_concurrency=4,
                parallel=lambda step: self.registry[step["capability"]].effect
                is AgentCapabilityEffect.READ,
            )
            result = {
                step_id: value if "flags" in value else model_result(value)
                for step_id, value in result.items()
            }
            await require_evidence_access(self.context)
            self.rounder.unpublished = plan_state.original
            self.rounder.unpublished_scope = plan_state.original_scope
            return result
        except AgentAccessLost:
            await discard_unpublished_results(self.context, plan_state.original, ())
            self.ledger.reports = plan_state.original_scope
            return {
                step["id"]: model_result({"error": "That lookup failed."}) for step in plan["steps"]
            }
