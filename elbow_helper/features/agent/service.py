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
from .access import (
    AgentAccessLost, AgentDisclosureDenied, require_access,
    require_access_requirements,
    require_destination_access, require_disclosure_access,
    require_evidence_access,
)
from .prompts import SYSTEM_PROMPT
from .conversation.context import compile_context, estimate_tokens
from .tools import build_agent_tools
from .usage import RequestUsage
from .budgets import ContextBudget
from .capabilities import (
    CapabilityBindError, compile_capability_call, require_source_provenance,
)
from .capabilities import CONTRACTS, SAVED_REPORT_CONTRACTS
from .tools.saved_reports import COMPARE_NAME, READ_NAME, original_arguments, original_tool
from .access import can_disclose_provenance
from .plan.checker import _kind, _periods, _source_check, _time_check, _valid_arguments, check_plan
from .plan.executor import execute_plan, resolve_arguments
from .plan.format import PLAN_TOOL_NAME, plan_definition, system_instructions
from .plan.planning import read_request
from .plan.results import model_result, plan_feedback
from .plan.sources import named_sources
from .plan.scope import ScopeLedger, resource_ids
from .commands.adapters import enabled_adapters
from .commands.bridge import build_command_tools, check_command_plan
from .commands.outcomes import command_reply
from .commands.confirmation import preview_text
from .wording import COMMAND_UNAVAILABLE
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
MIN_FINAL_OUTPUT_TOKENS = 1_024
MAX_SCOPE_REVISIONS = 2


def _scope_entries(plan: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(kind + json.dumps(value, sort_keys=True)
                     for kind in ("periods", "entities") for value in plan[kind])


class AgentUnavailableError(RuntimeError):
    """Raised when an agent request cannot produce a final answer."""


class AgentService:
    """Plan checked reads and answer from their results."""

    def __init__(self, model: AgentModel, *, commands_enabled: bool = False):
        self._model = model
        self._commands_enabled = commands_enabled

    @staticmethod
    async def _execute_tool(
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

    async def answer(
        self, *, question: str, local_context: str,
        context: AgentRequestContext, conversation_history: str = "",
    ) -> str:
        registry = build_agent_tools()
        command_capabilities = {}
        if self._commands_enabled:
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
        system_prompt = system_instructions(registry, commands_enabled=self._commands_enabled)
        context.state.request_text = question
        compiled = compile_context(
            question=question, local_context=local_context, context=context,
            tools=(definition,), conversation_history=conversation_history,
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
        started_at = time.monotonic()
        rounds = 0
        tool_calls = 0
        evidence_characters = 0
        status = "incomplete"
        completed: set[str] = set()
        state_lock = asyncio.Lock()
        ledger = ScopeLedger(context)
        unpublished = None
        unpublished_scope = None

        async def advance_model(results=(), *, allow_tools=True, reasoning_effort=AgentReasoningEffort.LOW,
                                max_output_tokens=INITIAL_MAX_OUTPUT_TOKENS):
            nonlocal rounds, unpublished, unpublished_scope
            try:
                await require_disclosure_access(context)
            except AgentAccessLost:
                if unpublished is None:
                    raise
                await _discard_unpublished_results(context, unpublished, results)
                ledger.reports = unpublished_scope
                results = tuple(AgentToolResult(item.call_id, json.dumps({
                    "flags": {"status": "failed", "limits": ["access_lost"]},
                    "instruction": "Answer using the remaining authorized context.",
                })) for item in results)
            unpublished = unpublished_scope = None
            projected = budget.projected_input(results)
            remaining = (max_output_tokens if budget.context_window_tokens is None else
                         min(max_output_tokens, budget.context_window_tokens - projected))
            if remaining < MIN_FINAL_OUTPUT_TOKENS:
                raise AgentUnavailableError("The agent lacks context room for an answer")
            if rounds >= MAX_MODEL_ROUNDS + 1:
                raise AgentUnavailableError("The agent reached its model-round limit")
            budget.output_reserve = remaining
            usage.attempted_rounds += 1
            rounds += 1
            round_started = time.monotonic()
            outcome = "failed"
            try:
                async with asyncio.timeout_at(context.deadline_monotonic):
                    model_step = await session.advance(
                        results, allow_tools=allow_tools, reasoning_effort=reasoning_effort,
                        max_output_tokens=remaining,
                    )
                usage.observe(model_step.usage)
                budget.observe(model_step.usage, projected_input=projected)
                outcome = "completed"
                LOGGER.info(
                    "Agent model response: request=%s round=%s model=%s provider_request_id=%s "
                    "provider_duration_ms=%s prompt_tokens=%s completion_tokens=%s cache_hit_tokens=%s cache_miss_tokens=%s",
                    request_id, rounds, model_step.model_identity, model_step.provider_request_id,
                    model_step.provider_duration_ms, model_step.usage.prompt_tokens,
                    model_step.usage.completion_tokens, model_step.usage.prompt_cache_hit_tokens,
                    model_step.usage.prompt_cache_miss_tokens,
                )
                return model_step
            finally:
                LOGGER.info("Agent model round: request=%s round=%s outcome=%s effort=%s output_limit=%s elapsed_ms=%s",
                            request_id, rounds, outcome, reasoning_effort.value, remaining,
                            int((time.monotonic() - round_started) * 1000))

        async def disclosure_issue(step: Mapping[str, Any]) -> str:
            selected = original_tool(step["capability"], step["arguments"])
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

        async def run_plan(plan: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
            nonlocal tool_calls, evidence_characters, context, unpublished, unpublished_scope
            original = _tool_state_snapshot(context)
            original_scope = dict(ledger.reports)
            emitted: list[AgentToolResult] = []
            periods = _periods(plan["periods"])
            entities: dict[str, set[str]] = {}
            for entity in plan["entities"]:
                if not isinstance(entity["value"], dict):
                    entities.setdefault(_kind(entity["kind"]), set()).add(str(entity["value"]))
            named = {_kind(kind): {str(value) for value in values}
                     for kind, values in sources.items() if values}

            async def run_one(step: Mapping[str, Any], arguments: Mapping[str, Any], earlier_results: Mapping[str, Any]) -> Mapping[str, Any]:
                nonlocal tool_calls, evidence_characters, context
                name = step["capability"]
                tool = registry[name]
                selected = original_tool(name, arguments) if name in (READ_NAME, COMPARE_NAME) else None
                contract = (SAVED_REPORT_CONTRACTS[selected.definition.name] if selected
                            else CONTRACTS.get(name))
                retained = []
                if not _valid_arguments(arguments, tool.definition.parameters):
                    return {"error": "Arguments must match the capability schema."}
                if name in (READ_NAME, COMPARE_NAME) and (
                    selected is None or not _valid_arguments(
                        original_arguments(arguments), selected.definition.parameters,
                    )
                ):
                    return {"error": "Use the fields supported by this report kind."}
                if tool.effect is AgentCapabilityEffect.COMMAND:
                    issue = check_command_plan(
                        {**plan, "steps": [{**step, "arguments": arguments}]},
                        command_capabilities, sources,
                    )
                    if issue:
                        return {"error": issue}
                bound_periods = []
                for kind, value, extra in periods:
                    if kind != "resolved":
                        bound_periods.append((kind, value, extra))
                        continue
                    if value == step["id"]:
                        continue
                    try:
                        key_value = earlier_results[value]
                        for part in extra:
                            key_value = key_value[part]
                    except (KeyError, IndexError, TypeError):
                        return {"error": "The declared period could not be resolved."}
                    for field, reference in step["arguments"].items():
                        if isinstance(reference, dict) and reference == {"step": value, "path": extra}:
                            bound_periods.append(("key", key_value, field))
                if contract is not None:
                    resolved_entities = {kind: set(values) for kind, values in entities.items()}
                    for entity in plan["entities"]:
                        if isinstance(entity["value"], dict):
                            try:
                                value = resolve_arguments({"value": entity["value"]}, earlier_results)["value"]
                            except (KeyError, IndexError, TypeError):
                                continue
                            resolved_entities.setdefault(_kind(entity["kind"]), set()).add(str(value))
                    retained = [identity for field in contract.retained_fields
                                if field in arguments for identity in (
                                    arguments[field] if isinstance(arguments[field], list)
                                    else [arguments[field]]
                                )]
                    issue = ledger.check(retained, tuple(bound_periods), named, resolved_entities)
                    if issue:
                        return {"error": issue}
                    bound = frozenset(named) | frozenset(
                        _kind(kind) for field, kind in contract.entity_fields if field in contract.retained_fields
                    ) if retained else frozenset()
                    issue, offered = _source_check(contract, arguments, named, resolved_entities, {}, bound)
                    if issue:
                        return {"error": issue, "offered": offered}
                    issue = _time_check(contract, arguments, tuple(bound_periods), set())
                    if issue:
                        return {"error": issue}
                try:
                    capability_scope = compile_capability_call(
                        selected or tool,
                        original_arguments(arguments) if selected else arguments,
                        contract=contract,
                    )
                except CapabilityBindError as error:
                    return {"error": str(error)}
                if retained:
                    capability_scope["bound_source_channels"] = sorted(ledger.channels(retained))
                issue = await disclosure_issue({**step, "arguments": arguments})
                if issue:
                    return {"error": issue}
                key = name + json.dumps(arguments, sort_keys=True, default=str)
                async with state_lock:
                    if key in completed:
                        return {"error": "This identical lookup already ran."}
                    if tool_calls >= MAX_TOOL_CALLS:
                        return {"error": "The request reached its lookup limit."}
                    if evidence_characters >= MAX_EVIDENCE_CHARACTERS:
                        return {"error": "The request reached its evidence limit."}
                    if context.deadline_monotonic is not None and (
                        time.monotonic() + (COMMAND_TIMEOUT_SECONDS if tool.effect is AgentCapabilityEffect.COMMAND
                                            else TOOL_TIMEOUT_SECONDS)
                        + FINAL_ANSWER_TIME_RESERVE_SECONDS
                        + DELIVERY_TIME_RESERVE_SECONDS >= context.deadline_monotonic
                    ):
                        return {"error": "The request has no time for another lookup."}
                    if not budget.can_continue_tools(budget.projected_input(emitted), result_reserve=8192):
                        return {"error": "The request has no context room for another lookup."}
                    tool_calls += 1
                    completed.add(key)
                    local = replace(context, state=_clone_tool_state(context.state))
                    previous = _tool_state_snapshot(local)
                local = replace(local, member=require_access(
                    local.guild, local.member.id, local.source_message.channel,
                ))
                await require_evidence_access(local)
                raw = await self._execute_tool(
                    name=name, handler=tool.handler, arguments=arguments,
                    capability_scope=capability_scope, context=local,
                    timeout_seconds=(COMMAND_TIMEOUT_SECONDS if tool.effect is AgentCapabilityEffect.COMMAND
                                     else TOOL_TIMEOUT_SECONDS),
                )
                try:
                    raw_payload = json.loads(raw)
                except ValueError:
                    raw_payload = {"error": "Invalid lookup result"}
                coverage_dates = {}
                if contract is not None and contract.time_window is not None:
                    coverage_dates = {
                        field: arguments[field] for field in contract.time_window[:2]
                        if field in arguments
                    }
                model_content = json.dumps(
                    model_result(raw_payload, coverage_dates=coverage_dates),
                    ensure_ascii=False, default=str, separators=(",", ":"),
                )
                async with state_lock:
                    limit = budget.result_character_limit(
                        emitted, pending_call_ids=(step["id"],),
                        maximum=min(MAX_TOOL_RESULT_CHARACTERS,
                                    MAX_EVIDENCE_CHARACTERS - evidence_characters),
                    )
                    content = _bound_tool_result(model_content, limit)
                    emitted.append(AgentToolResult(step["id"], content))
                    evidence_characters += len(content) if content else max(0, limit)
                    _merge_tool_state(context, local, previous)
                    for identity in local.state.reports:
                        if identity not in original["reports"]:
                            ledger.remember(identity, name, arguments)
                    for identity in resource_ids(raw_payload):
                        ledger.remember(identity, name, arguments)
                    context.state.evidence.append(_evidence_record(
                        call_id=step["id"], tool=name, arguments=arguments,
                        result=content, raw_result_characters=len(model_content),
                        result_complete=content == model_content,
                        capability_scope=capability_scope, context=context,
                    ))
                try:
                    return json.loads(content)
                except ValueError:
                    return {"error": "The result could not be included."}

            try:
                result = await execute_plan(
                    plan, run_one, max_concurrency=4,
                    parallel=lambda step: registry[step["capability"]].effect is AgentCapabilityEffect.READ,
                )
                result = {
                    step_id: value if "flags" in value else model_result(value)
                    for step_id, value in result.items()
                }
                await require_disclosure_access(context)
                unpublished = original
                unpublished_scope = original_scope
                return result
            except AgentAccessLost:
                await _discard_unpublished_results(context, original, ())
                ledger.reports = original_scope
                return {step["id"]: model_result({"error": "That lookup failed."})
                        for step in plan["steps"]}

        try:
            context = replace(context, member=require_access(
                context.guild, context.member.id, context.source_message.channel,
            ))
            await require_disclosure_access(context)
            decision = await read_request(
                session, registry, sources, request_id, validate_step=disclosure_issue,
                validate_plan=command_check,
                advance=advance_model,
            )
            if decision.answer is not None:
                await require_disclosure_access(context)
                status = "completed"
                return decision.answer
            plan = decision.plan
            revisions = 0
            correction_used = len(decision.rounds) > 1
            scope = _scope_entries(plan)
            while rounds < MAX_MODEL_ROUNDS:
                results = await run_plan(plan)
                if any(item.status == "needs_input" for item in context.state.command_outcomes):
                    context.state.command_proposals.clear()
                    status = "completed"
                    return command_reply(context.state.command_outcomes)
                expected_previews = sum(
                    command_capabilities[step["capability"]].adapter.delivery == "confirm"
                    for step in plan["steps"] if step["capability"] in command_capabilities
                )
                if expected_previews and len(context.state.command_proposals) != expected_previews:
                    context.state.command_proposals.clear()
                    status = "completed"
                    return COMMAND_UNAVAILABLE
                if context.state.command_proposals:
                    response = preview_text(context.state.command_proposals)
                    if context.state.command_outcomes:
                        response += "\n\n" + command_reply(context.state.command_outcomes)
                    await require_disclosure_access(context)
                    status = "completed"
                    return response
                if context.state.command_outcomes:
                    await require_disclosure_access(context)
                    status = "completed"
                    return command_reply(context.state.command_outcomes)
                pending = (AgentToolResult(
                    decision.rounds[-1].tool_calls[0].call_id,
                    json.dumps({"results": results, "instruction": "Answer now from these results. Submit another plan only for a remaining gap."},
                               ensure_ascii=False, default=str),
                ),)
                projected_input = budget.projected_input(pending)
                available_output = (
                    FINAL_MAX_OUTPUT_TOKENS if budget.context_window_tokens is None
                    else min(FINAL_MAX_OUTPUT_TOKENS,
                             budget.context_window_tokens - projected_input)
                )
                if available_output < MIN_FINAL_OUTPUT_TOKENS:
                    raise AgentUnavailableError("The agent lacks context room for an answer")
                budget.output_reserve = available_output
                allow_more = (
                    rounds < MAX_MODEL_ROUNDS - 1
                    and tool_calls < MAX_TOOL_CALLS
                    and evidence_characters < MAX_EVIDENCE_CHARACTERS
                    and budget.can_continue_tools(projected_input, result_reserve=8192)
                    and (context.deadline_monotonic is None or time.monotonic()
                         + MODEL_ROUND_TIME_RESERVE_SECONDS + TOOL_TIMEOUT_SECONDS
                         + FINAL_ANSWER_TIME_RESERVE_SECONDS + DELIVERY_TIME_RESERVE_SECONDS
                         < context.deadline_monotonic)
                )
                model_step = await advance_model(
                    pending, allow_tools=allow_more,
                    reasoning_effort=AgentReasoningEffort(plan["effort"]),
                    max_output_tokens=available_output,
                )
                if not model_step.tool_calls:
                    if not model_step.content:
                        raise AgentUnavailableError("The agent returned no final answer")
                    await require_disclosure_access(context)
                    status = "completed"
                    return model_step.content
                if not allow_more:
                    refusals = tuple(AgentToolResult(
                        call.call_id,
                        json.dumps({"flags": {"status": "refused", "reason": "answer_only"}}),
                    ) for call in model_step.tool_calls)
                    recovery = await advance_model(
                        refusals, allow_tools=False,
                        reasoning_effort=AgentReasoningEffort(plan["effort"]),
                        max_output_tokens=available_output,
                    )
                    if recovery.tool_calls or not recovery.content:
                        raise AgentUnavailableError("The agent did not answer after lookups ended")
                    await require_disclosure_access(context)
                    status = "completed"
                    return recovery.content
                if len(model_step.tool_calls) != 1 or model_step.tool_calls[0].name != PLAN_TOOL_NAME:
                    raise AgentUnavailableError("The agent requested an unknown planning action")
                try:
                    next_plan = json.loads(model_step.tool_calls[0].arguments)
                except (TypeError, ValueError):
                    raise AgentUnavailableError("The agent returned an invalid plan") from None
                check = check_plan(next_plan, registry, sources)
                if check.ok:
                    command_issue = command_check(next_plan)
                    if command_issue:
                        check = type(check)(False, command_issue)
                LOGGER.info("Agent plan: request=%s revision=%s plan=%s", request_id,
                            revisions + 1, json.dumps(next_plan, ensure_ascii=False, default=str))
                LOGGER.info("Agent plan check: request=%s revision=%s ok=%s step=%s error=%s",
                            request_id, revisions + 1, check.ok, check.step_id, check.error)
                if not check.ok:
                    if correction_used:
                        raise AgentUnavailableError(check.error)
                    correction_used = True
                    corrected = await advance_model((AgentToolResult(
                        model_step.tool_calls[0].call_id,
                        json.dumps(plan_feedback(check.error, step_id=check.step_id,
                                                 offered=check.offered)),
                    ),), reasoning_effort=AgentReasoningEffort(plan["effort"]))
                    if not corrected.tool_calls and corrected.content:
                        status = "completed"
                        return corrected.content
                    if len(corrected.tool_calls) != 1 or corrected.tool_calls[0].name != PLAN_TOOL_NAME:
                        raise AgentUnavailableError("A corrected plan is required")
                    next_plan = json.loads(corrected.tool_calls[0].arguments)
                    check = check_plan(next_plan, registry, sources)
                    LOGGER.info("Agent plan correction: request=%s ok=%s plan=%s error=%s",
                                request_id, check.ok, json.dumps(next_plan, ensure_ascii=False), check.error)
                    if not check.ok:
                        raise AgentUnavailableError(check.error)
                    model_step = corrected
                changed = _scope_entries(next_plan)
                if changed - scope:
                    revisions += 1
                    LOGGER.info("Agent scope revision: request=%s revision=%s", request_id, revisions)
                    if revisions > MAX_SCOPE_REVISIONS:
                        raise AgentUnavailableError("The request reached its scope revision limit")
                scope |= changed
                plan = next_plan
                decision = type(decision)(None, plan, (model_step,))
            raise AgentUnavailableError("The agent reached its model-round limit")
        except TextGenerationError as error:
            status = "provider_error"
            raise AgentUnavailableError(str(error)) from error
        except ValueError as error:
            status = "invalid_plan"
            raise AgentUnavailableError(str(error)) from error
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except AgentAccessLost:
            status = "access_lost"
            raise
        finally:
            self._log_completion(
                context=context, rounds=rounds, tool_calls=tool_calls,
                evidence_characters=evidence_characters, usage=usage,
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
