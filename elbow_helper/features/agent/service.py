"""Bounded orchestration for the agent."""

from __future__ import annotations

import asyncio
from dataclasses import fields, replace
from datetime import datetime, timezone
from collections.abc import Mapping, Sequence
import json
import logging
import sqlite3
import time
from typing import Any

import discord

from elbow_helper.infrastructure.ai import AgentModel
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from elbow_helper.infrastructure.ai import AgentToolResult
from elbow_helper.infrastructure.ai import TextGenerationError

from .models import AgentCapabilityEffect, AgentRequestContext
from .actions.contracts import ActionClass, PreparedAction
from .access import (
    AgentAccessLost, AgentDisclosureDenied, require_access,
    require_access_requirements,
    require_destination_access, require_disclosure_access,
    require_evidence_access,
)
from .prompts import SYSTEM_PROMPT
from .conversation.context import compile_context, estimate_tokens
from .engine.registry import build_agent_tools
from .engine.usage import RequestUsage
from .engine.budgets import ContextBudget
from .engine.capability_contract import CapabilityBindError
from .engine.capability_contract import compile_capability_call
from .engine.capability_contract import require_source_provenance
from .engine.capability_contract import CONTRACTS
from .engine.capability_contract import SAVED_REPORT_CONTRACTS
from .reports.tools import COMPARE_NAME, READ_NAME, filter_fields, original_arguments, original_tool
from .access import can_disclose_provenance
from .plan.checker import _has_reference, _kind, _periods, _source_check, _time_check, _valid_arguments, check_plan
from .plan.executor import execute_plan, resolve_arguments
from .plan.format import PLAN_TOOL_NAME, plan_definition, system_instructions
from .plan.planning import PlanNotSettled, read_request
from .plan.results import model_result, plan_feedback
from .plan.sources import named_sources
from .plan.scope import ScopeLedger, resource_ids
from .capabilities import enabled_adapters
from .commands.bridge import build_command_tools, check_command_plan
from .actions.outcomes import command_reply
from .actions.preview import preview_text
from .wording import (
    AGENT_ANSWER_UNFINISHED, AGENT_PLAN_UNFINISHED,
    AGENT_RESEARCH_UNFINISHED, COMMAND_UNAVAILABLE,
)
from .reports.base import retain_reports


LOGGER = logging.getLogger(__name__)
# Leave enough bounded continuations for large, paginated requests that combine
# several capability groups before the required final-answer round.
MAX_MODEL_ROUNDS = 20
MAX_TOOL_CALLS = 48
MAX_TOOL_RESULT_CHARACTERS = 64_000
MAX_EVIDENCE_CHARACTERS = 300_000
TOOL_TIMEOUT_SECONDS = 30.0
COMMAND_TIMEOUT_SECONDS = 180.0
MODEL_ROUND_TIME_RESERVE_SECONDS = 15.0
FINAL_ANSWER_TIME_RESERVE_SECONDS = 45.0
DELIVERY_TIME_RESERVE_SECONDS = 15.0
AGENT_MAX_OUTPUT_TOKENS = 64_000
INITIAL_MAX_OUTPUT_TOKENS = 8_000
FINAL_MAX_OUTPUT_TOKENS = 16_000
ANSWER_OUTPUT_LIMITS = {
    AgentReasoningEffort.LOW: FINAL_MAX_OUTPUT_TOKENS,
    AgentReasoningEffort.HIGH: 32_000,
    AgentReasoningEffort.MAX: 64_000,
}
MIN_FINAL_OUTPUT_TOKENS = 1_024
MAX_SCOPE_REVISIONS = 4


def _scope_entries(plan: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(kind + json.dumps(value, sort_keys=True)
                     for kind in ("periods", "entities") for value in plan[kind])


async def _disclosure_issue(context: AgentRequestContext,
                            registry: Mapping[str, Any],
                            step: Mapping[str, Any]) -> str:
    selected = original_tool(registry, step["capability"], step["arguments"])
    contract = (SAVED_REPORT_CONTRACTS[selected.definition.name] if selected
                else CONTRACTS.get(step["capability"]))
    if contract is None:
        return ""
    channels: set[int] = set()
    for field in contract.channel_fields:
        value = step["arguments"].get(field)
        if type(value) is int:
            channels.add(value)
        elif isinstance(value, list):
            channels.update(item for item in value if type(item) is int)
    if (channels or contract.required_access) and not await can_disclose_provenance(
        context, channels, contract.required_access,
    ):
        return "That source cannot be shared in this channel."
    return ""


class AgentUnavailableError(RuntimeError):
    """Raised when an agent request cannot produce a final answer."""


class AgentGracefulEnd(RuntimeError):
    """Return a bounded response when planning or answering cannot continue."""


class _ModelRounds:
    """Advance bounded model rounds and retain unpublished result state."""

    def __init__(self, *, context, budget, usage, session, request_id, ledger):
        self.context = context
        self.budget = budget
        self.usage = usage
        self.session = session
        self.request_id = request_id
        self.ledger = ledger
        self.rounds = 0
        self.unpublished = None
        self.unpublished_scope = None

    async def advance(self, results=(), *, allow_tools=True, reasoning_effort=AgentReasoningEffort.LOW, max_output_tokens=INITIAL_MAX_OUTPUT_TOKENS, continuation_instruction=None, continued=False):
        try:
            await require_disclosure_access(self.context)
        except AgentAccessLost:
            if self.unpublished is None:
                raise
            await _discard_unpublished_results(self.context, self.unpublished, results)
            self.ledger.reports = self.unpublished_scope
            results = tuple((AgentToolResult(item.call_id, json.dumps({'flags': {'status': 'failed', 'limits': ['access_lost']}, 'instruction': 'Answer using the remaining authorized context.'})) for item in results))
        self.unpublished = self.unpublished_scope = None
        projected = self.budget.projected_input(results) + estimate_tokens(continuation_instruction or '')
        remaining = max_output_tokens if self.budget.context_window_tokens is None else min(max_output_tokens, self.budget.context_window_tokens - projected)
        if remaining < MIN_FINAL_OUTPUT_TOKENS:
            raise AgentGracefulEnd(AGENT_RESEARCH_UNFINISHED)
        if self.rounds >= MAX_MODEL_ROUNDS + 1:
            raise AgentGracefulEnd(AGENT_RESEARCH_UNFINISHED)
        self.budget.output_reserve = remaining
        self.usage.attempted_rounds += 1
        self.rounds += 1
        round_started = time.monotonic()
        outcome = 'failed'
        try:
            async with asyncio.timeout_at(self.context.deadline_monotonic):
                model_step = await self.session.advance(results, allow_tools=allow_tools, reasoning_effort=reasoning_effort, max_output_tokens=remaining, **{'continuation_instruction': continuation_instruction} if continuation_instruction else {})
            self.usage.observe(model_step.usage)
            self.budget.observe(model_step.usage, projected_input=projected)
            outcome = 'completed'
            LOGGER.info('Agent model response: request=%s round=%s model=%s provider_request_id=%s provider_duration_ms=%s prompt_tokens=%s completion_tokens=%s cache_hit_tokens=%s cache_miss_tokens=%s', self.request_id, self.rounds, model_step.model_identity, model_step.provider_request_id, model_step.provider_duration_ms, model_step.usage.prompt_tokens, model_step.usage.completion_tokens, model_step.usage.prompt_cache_hit_tokens, model_step.usage.prompt_cache_miss_tokens)
            if model_step.output_limit_reached:
                LOGGER.warning('Agent model output limit reached: request=%s round=%s limit=%s', self.request_id, self.rounds, remaining)
                if continued:
                    raise AgentGracefulEnd(AGENT_ANSWER_UNFINISHED)
                incomplete_calls = tuple((AgentToolResult(call.call_id, json.dumps({'error': 'The prior model output was incomplete. Submit the full plan again.'})) for call in model_step.tool_calls))
                instruction = 'Submit the full tool call again; the previous one was incomplete.' if incomplete_calls else 'Continue the previous answer from where it stopped. Do not repeat it.'
                continuation = await self.advance(incomplete_calls, allow_tools=allow_tools, reasoning_effort=reasoning_effort, max_output_tokens=max_output_tokens, continuation_instruction=instruction, continued=True)
                if incomplete_calls or continuation.tool_calls:
                    return continuation
                return replace(continuation, content=model_step.content + '\n' + continuation.content if model_step.content else continuation.content)
            return model_step
        finally:
            LOGGER.info('Agent model round: request=%s round=%s outcome=%s effort=%s output_limit=%s elapsed_ms=%s', self.request_id, self.rounds, outcome, reasoning_effort.value, remaining, int((time.monotonic() - round_started) * 1000))


class _PlanRunner:
    """Run checked steps and retain bounded evidence for the answer round."""

    def __init__(self, *, service, context, registry, command_capabilities,
                 sources, budget, ledger, rounder):
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

    async def run_one(self, step: Mapping[str, Any], arguments: Mapping[str, Any],
                      earlier_results: Mapping[str, Any]) -> Mapping[str, Any]:
        checked = await self._check_step(step, arguments, earlier_results)
        if "error" in checked:
            return checked
        reserved = await self._reserve_tool(step, arguments, checked["tool"])
        if isinstance(reserved, dict):
            return reserved
        local, previous = reserved
        return await self._execute_checked(step, arguments, checked, local, previous)

    async def _check_step(self, step, arguments, earlier_results):
        name = step["capability"]
        tool = self.registry[name]
        selected = (original_tool(self.registry, name, arguments)
                    if name in (READ_NAME, COMPARE_NAME) else None)
        contract = (SAVED_REPORT_CONTRACTS[selected.definition.name] if selected
                    else CONTRACTS.get(name))
        dependencies = set(step["depends_on"])
        if not _valid_arguments(arguments, tool.definition.parameters, dependencies):
            return {"error": "Arguments must match the capability schema."}
        if name in (READ_NAME, COMPARE_NAME) and (
            selected is None or not _valid_arguments(
                original_arguments(arguments), selected.definition.parameters, dependencies,
            )
        ):
            return {"error": "Use the fields supported by this report kind."}
        if tool.effect is AgentCapabilityEffect.COMMAND:
            issue = check_command_plan(
                {**self.plan, "steps": [{**step, "arguments": arguments}]},
                self.command_capabilities, self.sources,
            )
            if issue:
                return {"error": issue}
        try:
            bound_periods = self._bound_periods(step, earlier_results)
        except (KeyError, IndexError, TypeError):
            return {"error": "The declared period could not be resolved."}
        retained = []
        if contract is not None:
            resolved_entities = self._resolved_entities(earlier_results)
            retained = [identity for field in contract.retained_fields
                        if field in arguments for identity in (
                            arguments[field] if isinstance(arguments[field], list)
                            else [arguments[field]]
                        )]
            issue = self.ledger.check(retained, bound_periods, self.named,
                                      resolved_entities)
            if issue:
                return {"error": issue}
            bound = (frozenset(self.named) | frozenset(
                _kind(kind) for field, kind in contract.entity_fields
                if field in contract.retained_fields
            )) if retained else frozenset()
            references = {field: value for field, value in arguments.items()
                          if _has_reference(value)}
            issue, offered = _source_check(contract, arguments, self.named,
                                           resolved_entities, references, bound)
            if issue:
                return {"error": issue, "offered": offered}
            issue = _time_check(contract, arguments, bound_periods, dependencies)
            if issue:
                return {"error": issue}
        try:
            scope = compile_capability_call(
                selected or tool,
                original_arguments(arguments) if selected else arguments,
                contract=contract,
            )
        except CapabilityBindError as error:
            return {"error": str(error)}
        if retained:
            scope["bound_source_channels"] = sorted(self.ledger.channels(retained))
        issue = await _disclosure_issue(self.context, self.registry,
                                        {**step, "arguments": arguments})
        if issue:
            return {"error": issue}
        return {"name": name, "tool": tool, "contract": contract, "scope": scope}

    def _bound_periods(self, step, earlier_results):
        bound = []
        for kind, value, extra in self.periods:
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

    def _resolved_entities(self, earlier_results):
        resolved = {kind: set(values) for kind, values in self.entities.items()}
        for entity in self.plan["entities"]:
            if isinstance(entity["value"], dict):
                try:
                    value = resolve_arguments({"value": entity["value"]},
                                              earlier_results)["value"]
                except (KeyError, IndexError, TypeError):
                    continue
                resolved.setdefault(_kind(entity["kind"]), set()).add(str(value))
        return resolved

    async def _reserve_tool(self, step, arguments, tool):
        key = step["capability"] + json.dumps(arguments, sort_keys=True, default=str)
        async with self.state_lock:
            if key in self.completed:
                return {"error": "This identical lookup already ran."}
            if self.tool_calls >= MAX_TOOL_CALLS:
                return {"error": "The request reached its lookup limit."}
            if self.evidence_characters >= MAX_EVIDENCE_CHARACTERS:
                return {"error": "The request reached its evidence limit."}
            timeout = (COMMAND_TIMEOUT_SECONDS if tool.effect is AgentCapabilityEffect.COMMAND
                       else TOOL_TIMEOUT_SECONDS)
            if self.context.deadline_monotonic is not None and (
                time.monotonic() + timeout + FINAL_ANSWER_TIME_RESERVE_SECONDS
                + DELIVERY_TIME_RESERVE_SECONDS >= self.context.deadline_monotonic
            ):
                return {"error": "The request has no time for another lookup."}
            if not self.budget.can_continue_tools(
                self.budget.projected_input(self.emitted), result_reserve=8192,
            ):
                return {"error": "The request has no context room for another lookup."}
            self.tool_calls += 1
            self.completed.add(key)
            local = replace(self.context, state=_clone_tool_state(self.context.state))
            previous = _tool_state_snapshot(local)
        return local, previous

    async def _execute_checked(self, step, arguments, checked, local, previous):
        local = replace(local, member=require_access(
            local.guild, local.member.id, local.source_message.channel,
        ))
        await require_evidence_access(local)
        tool = checked["tool"]
        raw = await self.service.execute_tool(
            name=checked["name"], handler=tool.handler, arguments=arguments,
            capability_scope=checked["scope"], context=local,
            timeout_seconds=(COMMAND_TIMEOUT_SECONDS
                             if tool.effect is AgentCapabilityEffect.COMMAND
                             else TOOL_TIMEOUT_SECONDS),
        )
        for index in range(len(previous["command_proposals"]),
                           len(local.state.command_proposals)):
            proposal = local.state.command_proposals[index]
            if isinstance(proposal, PreparedAction):
                local.state.command_proposals[index] = replace(
                    proposal, step_id=step["id"],
                )
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"error": "Invalid lookup result"}
        contract = checked["contract"]
        coverage_dates = {}
        if contract is not None and contract.time_window is not None:
            coverage_dates = {field: arguments[field] for field in contract.time_window[:2]
                              if field in arguments}
        model_content = json.dumps(
            model_result(payload, coverage_dates=coverage_dates),
            ensure_ascii=False, default=str, separators=(",", ":"),
        )
        return await self._record_result(step, arguments, checked["scope"],
                                         local, previous, payload, model_content)

    async def _record_result(self, step, arguments, scope, local, previous,
                             payload, model_content):
        async with self.state_lock:
            limit = self.budget.result_character_limit(
                self.emitted, pending_call_ids=(step["id"],),
                maximum=min(MAX_TOOL_RESULT_CHARACTERS,
                            MAX_EVIDENCE_CHARACTERS - self.evidence_characters),
            )
            content = _bound_tool_result(model_content, limit)
            self.emitted.append(AgentToolResult(step["id"], content))
            self.evidence_characters += len(content) if content else max(0, limit)
            _merge_tool_state(self.context, local, previous)
            for identity in local.state.reports:
                if identity not in self.original["reports"]:
                    self.ledger.remember(identity, step["capability"], arguments)
            for identity in resource_ids(payload):
                self.ledger.remember(identity, step["capability"], arguments)
            self.context.state.evidence.append(_evidence_record(
                call_id=step["id"], tool=step["capability"], arguments=arguments,
                result=content, raw_result_characters=len(model_content),
                result_complete=content == model_content,
                capability_scope=scope, context=self.context,
            ))
        try:
            return json.loads(content)
        except ValueError:
            return {"error": "The result could not be included."}

    async def run(self, plan: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
        self.plan = plan
        self.original = _tool_state_snapshot(self.context)
        self.original_scope = dict(self.ledger.reports)
        self.emitted: list[AgentToolResult] = []
        self.periods = _periods(self.plan['periods'])
        self.entities: dict[str, set[str]] = {}
        for entity in self.plan['entities']:
            if not isinstance(entity['value'], dict):
                self.entities.setdefault(_kind(entity['kind']), set()).add(str(entity['value']))
        self.named = {_kind(kind): {str(value) for value in values} for kind, values in self.sources.items() if values}
        try:
            result = await execute_plan(self.plan, self.run_one, max_concurrency=4, parallel=lambda step: self.registry[step['capability']].effect is AgentCapabilityEffect.READ)
            result = {step_id: value if 'flags' in value else model_result(value) for step_id, value in result.items()}
            await require_disclosure_access(self.context)
            self.rounder.unpublished = self.original
            self.rounder.unpublished_scope = self.original_scope
            return result
        except AgentAccessLost:
            await _discard_unpublished_results(self.context, self.original, ())
            self.ledger.reports = self.original_scope
            return {step['id']: model_result({'error': 'That lookup failed.'}) for step in self.plan['steps']}


class _AnswerFlow:
    """Read a request, run checked steps, and shape its final reply."""

    def __init__(self, *, context, session, registry, sources, command_check,
                 budget, rounder, runner, request_id):
        self.context = context
        self.session = session
        self.registry = registry
        self.sources = sources
        self.command_check = command_check
        self.budget = budget
        self.rounder = rounder
        self.runner = runner
        self.request_id = request_id

    async def run(self) -> str:
        self.context = replace(self.context, member=require_access(
            self.context.guild, self.context.member.id,
            self.context.source_message.channel,
        ))
        await require_disclosure_access(self.context)
        self.rounder.context = self.runner.context = self.context
        self.decision = await read_request(
            self.session, self.registry, self.sources, self.request_id,
            validate_step=lambda step: _disclosure_issue(self.context, self.registry, step),
            validate_plan=self.command_check, advance=self.rounder.advance,
        )
        if self.decision.answer is not None:
            await require_disclosure_access(self.context)
            return self.decision.answer
        self.plan = self.decision.plan
        self.revisions = 0
        self.correction_used = len(self.decision.rounds) > 1
        self.scope = _scope_entries(self.plan)
        self._reserve_answer()
        while self.rounder.rounds < MAX_MODEL_ROUNDS:
            results = await self.runner.run(self.plan)
            response = await self._result_response(results)
            if response is not None:
                return response
            response, model_step = await self._answer_round(results)
            if response is not None:
                return response
            response = await self._revise(model_step)
            if response is not None:
                return response
        raise AgentGracefulEnd(AGENT_RESEARCH_UNFINISHED)

    def _reserve_answer(self) -> None:
        self.budget.final_answer_reserve = ANSWER_OUTPUT_LIMITS[
            AgentReasoningEffort(self.plan["effort"])
        ]

    async def _result_response(self, results: Mapping[str, Any]) -> str | None:
        state = self.context.state
        if any(item.status == "needs_input" for item in state.command_outcomes):
            state.command_proposals.clear()
            missing = [item for item in state.command_outcomes
                       if item.status == "needs_input"]
            options = list({option["name"]: option for item in missing
                            for option in item.missing_options}.values())
            hints = list(dict.fromkeys(description for item in missing
                                      for description in item.missing))
            pending = (AgentToolResult(
                self.decision.rounds[-1].tool_calls[0].call_id,
                json.dumps({
                    "results": results, "missing_options": options,
                    "missing_hints": hints,
                    "instruction": (
                        "Ask the member for all missing values together in your own words. "
                        "Use the option descriptions and choices as data. Suggest only values "
                        "the data supports. Do not say the command ran."
                    ),
                }, ensure_ascii=False, default=str),
            ),)
            reply = await self.rounder.advance(
                pending, allow_tools=False, reasoning_effort=AgentReasoningEffort.LOW,
                max_output_tokens=FINAL_MAX_OUTPUT_TOKENS,
            )
            if reply.tool_calls or not reply.content:
                raise AgentGracefulEnd(AGENT_PLAN_UNFINISHED)
            await require_disclosure_access(self.context)
            return reply.content
        expected = sum(
            results[step["id"]].get("prepared_count", 1)
            for step in self.plan["steps"]
            if self.registry[step["capability"]].action_class in (
                ActionClass.CHANGE, ActionClass.IRREVERSIBLE,
            )
        )
        if expected and len(state.command_proposals) != expected:
            state.command_proposals.clear()
            return COMMAND_UNAVAILABLE
        if state.command_proposals:
            response = preview_text(state.command_proposals)
            if state.command_outcomes:
                response += "\n\n" + command_reply(state.command_outcomes)
            await require_disclosure_access(self.context)
            return response
        if state.command_outcomes:
            await require_disclosure_access(self.context)
            return command_reply(state.command_outcomes)
        return None

    async def _answer_round(self, results: Mapping[str, Any]) -> tuple[str | None, Any]:
        pending = (AgentToolResult(
            self.decision.rounds[-1].tool_calls[0].call_id,
            json.dumps({"results": results,
                        "instruction": "Answer now from these results. Submit another plan only for a remaining gap."},
                       ensure_ascii=False, default=str),
        ),)
        projected = self.budget.projected_input(pending)
        answer_limit = ANSWER_OUTPUT_LIMITS[AgentReasoningEffort(self.plan["effort"])]
        available = (answer_limit if self.budget.context_window_tokens is None
                     else min(answer_limit, self.budget.context_window_tokens - projected))
        if available < MIN_FINAL_OUTPUT_TOKENS:
            raise AgentGracefulEnd(AGENT_RESEARCH_UNFINISHED)
        self.budget.output_reserve = available
        allow_more = (
            self.rounder.rounds < MAX_MODEL_ROUNDS - 1
            and self.runner.tool_calls < MAX_TOOL_CALLS
            and self.runner.evidence_characters < MAX_EVIDENCE_CHARACTERS
            and self.budget.can_continue_tools(projected, result_reserve=8192)
            and (self.context.deadline_monotonic is None or time.monotonic()
                 + MODEL_ROUND_TIME_RESERVE_SECONDS + TOOL_TIMEOUT_SECONDS
                 + FINAL_ANSWER_TIME_RESERVE_SECONDS + DELIVERY_TIME_RESERVE_SECONDS
                 < self.context.deadline_monotonic)
        )
        effort = AgentReasoningEffort(self.plan["effort"])
        step = await self.rounder.advance(
            pending, allow_tools=allow_more, reasoning_effort=effort,
            max_output_tokens=available,
        )
        if not step.tool_calls:
            if not step.content:
                raise AgentGracefulEnd(AGENT_ANSWER_UNFINISHED)
            await require_disclosure_access(self.context)
            return step.content, None
        if not allow_more:
            refusals = tuple(AgentToolResult(
                call.call_id,
                json.dumps({"flags": {"status": "refused", "reason": "answer_only"}}),
            ) for call in step.tool_calls)
            recovery = await self.rounder.advance(
                refusals, allow_tools=False, reasoning_effort=effort,
                max_output_tokens=available,
            )
            if recovery.tool_calls or not recovery.content:
                raise AgentGracefulEnd(AGENT_RESEARCH_UNFINISHED)
            await require_disclosure_access(self.context)
            return recovery.content, None
        return None, step

    async def _revise(self, model_step: Any) -> str | None:
        if len(model_step.tool_calls) != 1 or model_step.tool_calls[0].name != PLAN_TOOL_NAME:
            raise AgentGracefulEnd(AGENT_PLAN_UNFINISHED)
        try:
            next_plan = json.loads(model_step.tool_calls[0].arguments)
        except (TypeError, ValueError):
            raise AgentGracefulEnd(AGENT_PLAN_UNFINISHED) from None
        check = check_plan(next_plan, self.registry, self.sources)
        if check.ok:
            issue = self.command_check(next_plan)
            if issue:
                check = type(check)(False, issue)
        LOGGER.info("Agent plan: request=%s revision=%s plan=%s", self.request_id,
                    self.revisions + 1, json.dumps(next_plan, ensure_ascii=False, default=str))
        LOGGER.info("Agent plan check: request=%s revision=%s ok=%s step=%s error=%s",
                    self.request_id, self.revisions + 1, check.ok, check.step_id, check.error)
        if not check.ok:
            if self.correction_used:
                raise AgentGracefulEnd(AGENT_PLAN_UNFINISHED)
            self.correction_used = True
            corrected = await self.rounder.advance((AgentToolResult(
                model_step.tool_calls[0].call_id,
                json.dumps(plan_feedback(check.error, step_id=check.step_id,
                                         offered=check.offered)),
            ),), reasoning_effort=AgentReasoningEffort(self.plan["effort"]))
            if not corrected.tool_calls and corrected.content:
                return corrected.content
            if len(corrected.tool_calls) != 1 or corrected.tool_calls[0].name != PLAN_TOOL_NAME:
                raise AgentGracefulEnd(AGENT_PLAN_UNFINISHED)
            next_plan = json.loads(corrected.tool_calls[0].arguments)
            check = check_plan(next_plan, self.registry, self.sources)
            LOGGER.info("Agent plan correction: request=%s ok=%s plan=%s error=%s",
                        self.request_id, check.ok,
                        json.dumps(next_plan, ensure_ascii=False), check.error)
            if not check.ok:
                raise AgentGracefulEnd(AGENT_PLAN_UNFINISHED)
            model_step = corrected
        changed = _scope_entries(next_plan)
        if changed - self.scope:
            self.revisions += 1
            LOGGER.info("Agent scope revision: request=%s revision=%s",
                        self.request_id, self.revisions)
            if self.revisions > MAX_SCOPE_REVISIONS:
                raise AgentGracefulEnd(AGENT_RESEARCH_UNFINISHED)
        self.scope |= changed
        self.plan = next_plan
        self._reserve_answer()
        self.decision = type(self.decision)(None, self.plan, (model_step,))
        return None


class AgentService:
    """Plan checked reads and answer from their results."""

    def __init__(self, model: AgentModel, *, actions_enabled: bool = True):
        self._model = model
        self._actions_enabled = actions_enabled

    @staticmethod
    async def execute_tool(
        *,
        name: str,
        handler: Any,
        arguments: Mapping[str, Any],
        capability_scope: Mapping[str, Any] | None = None,
        context: AgentRequestContext,
        timeout_seconds: float = TOOL_TIMEOUT_SECONDS,
    ) -> str:
        snapshot = _tool_state_snapshot(context)
        started_at = time.monotonic()
        outcome = "failed"
        result_characters = 0
        try:
            required_access = frozenset((capability_scope or {}).get("required_access", ()))
            if required_access:
                require_access_requirements(
                    context.guild, context.member.id, required_access,
                )
            async with asyncio.timeout(timeout_seconds):
                payload = await handler(context, arguments)
            if not isinstance(payload, Mapping):
                raise CapabilityBindError("The lookup did not return a structured result.")
            failed = "error" in payload or payload.get("flags", {}).get("status") == "failed"
            if not failed:
                context.state.required_access.update(required_access)
                require_source_provenance(
                    capability_scope or {}, arguments,
                    context.state.source_channels, payload,
                )
            _record_report_provenance(context, snapshot["reports"])
            sources = await require_evidence_access(context)
            require_destination_access(context, sources)
            outcome = "completed"
            content = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))
            result_characters = len(content)
            return content
        except AgentDisclosureDenied:
            outcome = "disclosure_denied"
            _restore_tool_state(context, snapshot)
            return _error_result("That source cannot be shared in this channel.")
        except CapabilityBindError:
            outcome = "unbound_source"
            _restore_tool_state(context, snapshot)
            LOGGER.warning("Agent tool omitted source provenance: tool=%s", name)
            return _error_result("That lookup failed.")
        except AgentAccessLost:
            outcome = "access_lost"
            _restore_tool_state(context, snapshot)
            raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            _restore_tool_state(context, snapshot)
            raise
        except asyncio.TimeoutError:
            outcome = "timed_out"
            _restore_tool_state(context, snapshot)
            LOGGER.warning(
                "Agent tool timed out: tool=%s invoker=%s",
                name,
                context.member.id,
            )
            return _error_result("That lookup timed out.")
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
            _restore_tool_state(context, snapshot)
            LOGGER.exception(
                "Agent tool failed: tool=%s invoker=%s",
                name,
                context.member.id,
            )
            return _error_result("That lookup failed.")
        except BaseException:
            outcome = "failed"
            _restore_tool_state(context, snapshot)
            raise
        finally:
            LOGGER.info(
                "Agent tool: tool=%s invoker=%s outcome=%s elapsed_ms=%s request=%s result_chars=%s",
                name, context.member.id, outcome,
                int((time.monotonic() - started_at) * 1_000),
                getattr(context.source_message, "id", None), result_characters,
            )

    _execute_tool = execute_tool


    @staticmethod
    def _log_completion(
        *,
        context: AgentRequestContext,
        rounds: int,
        tool_calls: int,
        evidence_characters: int,
        usage: RequestUsage,
        status: str,
        elapsed_ms: int,
    ) -> None:
        LOGGER.info(
            "Agent usage: request=%s status=%s elapsed_ms=%s invoker=%s channel=%s rounds=%s tools=%s evidence_chars=%s "
            "prompt_tokens=%s completion_tokens=%s cache_hit_tokens=%s cache_miss_tokens=%s "
            "attempted_rounds=%s unknown_token_rounds=%s unknown_cache_rounds=%s",
            getattr(context.source_message, "id", None), status, elapsed_ms, context.member.id,
            context.source_message.channel.id,
            rounds,
            tool_calls,
            evidence_characters,
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.cache_hit_tokens,
            usage.cache_miss_tokens,
            usage.attempted_rounds,
            usage.unknown_token_rounds,
            usage.unknown_cache_rounds,
        )

    def _start_flow(self, *, question: str, local_context: str,
                    context: AgentRequestContext, conversation_history: str):
        registry = build_agent_tools()
        command_capabilities = {}
        actions_available = self._actions_enabled and getattr(context.bot, "tree", None) is not None
        if not actions_available:
            registry = {name: tool for name, tool in registry.items()
                        if tool.action_class not in (ActionClass.CHANGE, ActionClass.IRREVERSIBLE)}
        if actions_available:
            command_tools, command_capabilities = build_command_tools(
                context.bot, enabled_adapters(),
            )
            registry.update(command_tools)
        visible_channels = (
            *getattr(context.guild, "channels", ()),
            *getattr(context.guild, "threads", ()),
        )
        sources = named_sources(question, visible_channels)
        command_check = lambda plan: check_command_plan(plan, command_capabilities, sources)
        definition = plan_definition(registry)
        system_prompt = system_instructions(registry, actions_enabled=actions_available)
        context.state.request_text = question
        compiled = compile_context(
            question=question, local_context=local_context, context=context,
            tools=(definition,), conversation_history=conversation_history,
            report_filter_fields=filter_fields(registry),
        )
        request_prompt = compiled.prompt + "\nCurrent UTC: " + datetime.now(timezone.utc).isoformat()
        session = self._model.create_agent_session(
            system_prompt=system_prompt, prompt=request_prompt,
            tools=(definition,), max_output_tokens=AGENT_MAX_OUTPUT_TOKENS,
        )
        if session is None:
            raise AgentUnavailableError("The AI backend is not configured")
        budget = ContextBudget(
            context_window_tokens=getattr(session, "context_window_tokens", None),
            output_reserve=INITIAL_MAX_OUTPUT_TOKENS,
            next_input_estimate=compiled.estimated_input_tokens
                + estimate_tokens(system_prompt) - estimate_tokens(SYSTEM_PROMPT)
                + estimate_tokens(request_prompt) - estimate_tokens(compiled.prompt),
            final_answer_reserve=FINAL_MAX_OUTPUT_TOKENS,
        )
        usage = RequestUsage()
        request_id = getattr(context.source_message, "id", None)
        ledger = ScopeLedger(context)
        rounder = _ModelRounds(context=context, budget=budget, usage=usage,
                               session=session, request_id=request_id, ledger=ledger)


        runner = _PlanRunner(
            service=self, context=context, registry=registry,
            command_capabilities=command_capabilities, sources=sources,
            budget=budget, ledger=ledger, rounder=rounder,
        )


        flow = _AnswerFlow(
            context=context, session=session, registry=registry, sources=sources,
            command_check=command_check, budget=budget, rounder=rounder,
            runner=runner, request_id=request_id,
        )
        return flow, usage

    async def answer(
        self, *, question: str, local_context: str,
        context: AgentRequestContext, conversation_history: str = "",
    ) -> str:
        flow, usage = self._start_flow(
            question=question, local_context=local_context,
            context=context, conversation_history=conversation_history,
        )
        started_at = time.monotonic()
        status = "incomplete"
        rounder = flow.rounder
        runner = flow.runner
        try:
            answer = await flow.run()
            status = "completed"
            return answer
        except AgentGracefulEnd as error:
            await require_disclosure_access(flow.context)
            status = "incomplete"
            return str(error)
        except TextGenerationError as error:
            status = "provider_error"
            raise AgentUnavailableError(str(error)) from error
        except PlanNotSettled as error:
            LOGGER.warning("Agent plan not settled: request=%s reason=%s",
                           getattr(flow.context.source_message, "id", None), error)
            await require_disclosure_access(flow.context)
            status = "incomplete"
            return AGENT_PLAN_UNFINISHED
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except AgentAccessLost:
            status = "access_lost"
            raise
        finally:
            self._log_completion(
                context=flow.context, rounds=rounder.rounds, tool_calls=runner.tool_calls,
                evidence_characters=runner.evidence_characters, usage=usage,
                status=status, elapsed_ms=int((time.monotonic() - started_at) * 1000),
            )


def _evidence_record(
    *, call_id: str, tool: str, arguments: Mapping[str, Any],
    result: str, raw_result_characters: int, result_complete: bool,
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
    elif not result_complete or (isinstance(payload, dict) and (
        payload.get("truncated") or payload.get("flags", {}).get("status") == "partial"
    )):
        status = "partial"
    else:
        status = "complete"
    return json.dumps({
        "call_id": call_id,
        "tool": tool,
        "arguments": arguments,
        "result": result,
        "result_status": status,
        "raw_result_characters": raw_result_characters,
        "result_complete": status == "complete",
        "capability_scope": dict(capability_scope or {
            "precision": "schema_only", "capability": tool,
        }),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "source_channels": sorted(context.state.source_channels),
        "required_access": sorted(context.state.required_access),
    }, ensure_ascii=False)


def _record_report_provenance(
    context: AgentRequestContext,
    previous: Mapping[str, Any],
) -> None:
    """Bind new or replaced artifacts to all evidence access used so far."""
    for report_id, report in context.state.reports.items():
        if previous.get(report_id) is not report:
            context.state.report_sources[report_id] = frozenset(
                context.state.source_channels
            )
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


def _clone_tool_state(state):
    return replace(state, **{
        field.name: value.copy() for field in fields(state)
        if isinstance(value := getattr(state, field.name), (dict, list, set))
    })


def _merge_tool_state(target: AgentRequestContext, local: AgentRequestContext, previous: Mapping[str, Any]) -> None:
    additions = tuple(report for key, report in local.state.reports.items()
                      if previous["reports"].get(key) is not report)
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
    target.state.command_outcomes.extend(
        local.state.command_outcomes[len(previous["command_outcomes"]):]
    )
    target.state.command_proposals.extend(
        local.state.command_proposals[len(previous["command_proposals"]):]
    )


def _tool_state_snapshot(context: AgentRequestContext) -> dict[str, Any]:
    """Capture request-local state that a failed tool must not partially mutate."""
    return {
        field.name: value.copy() if isinstance(value, (dict, list, set)) else value
        for field in fields(context.state)
        for value in (getattr(context.state, field.name),)
    }


def _restore_tool_state(
    context: AgentRequestContext, snapshot: Mapping[str, Any],
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


async def _discard_unpublished_results(
    context: AgentRequestContext,
    snapshot: Mapping[str, Any],
    results: Sequence[AgentToolResult],
) -> list[AgentToolResult]:
    """Discard unsent evidence; never continue with revoked model context."""
    _restore_tool_state(context, snapshot)
    # This includes rollout eligibility, the request channel, earlier evidence and
    # role requirements. Loss of anything already in the prompt still aborts.
    await require_evidence_access(context)
    LOGGER.warning(
        "Agent unpublished evidence discarded: request=%s invoker=%s results=%s",
        getattr(context.source_message, "id", None), context.member.id, len(results),
    )
    return [
        AgentToolResult(result.call_id, _error_result("That lookup failed."))
        for result in results
    ]


def _bound_tool_result(content: str, limit: int) -> str:
    if limit < 2:
        return ""
    if len(content) <= limit:
        return content
    # JSON escaping can expand an excerpt; measure the encoded envelope itself.
    low, high = 0, len(content)
    result = "{}"
    while low <= high:
        midpoint = (low + high) // 2
        candidate = json.dumps({"truncated": True, "result_excerpt": content[:midpoint]}, ensure_ascii=False)
        if len(candidate) <= limit:
            result = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return result


def _error_result(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)
