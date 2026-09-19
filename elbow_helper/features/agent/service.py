"""Bounded orchestration for the Core read-only agent."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from collections.abc import Mapping, Sequence
import json
import logging
import sqlite3
import time
from typing import Any

import discord

from elbow_helper.infrastructure.ai import AgentModel
from elbow_helper.infrastructure.ai import AgentToolResult
from elbow_helper.infrastructure.ai import TextGenerationError

from .models import AgentRequestContext
from .access import AgentAccessLost, require_access, require_evidence_access
from .prompts import SYSTEM_PROMPT
from .conversation.context import compile_context, estimate_tokens
from .tools import build_agent_tool_groups, build_agent_tools
from .tool_selection import (
    DISCOVERY_TOOL_NAME, ToolSelection, encoded_definitions,
)
from .usage import RequestUsage
from .budgets import ContextBudget


LOGGER = logging.getLogger(__name__)
# Leave enough bounded continuations for large, paginated requests that combine
# several capability groups before the required final-answer round.
MAX_MODEL_ROUNDS = 20
MAX_TOOL_CALLS = 48
MAX_TOOL_RESULT_CHARACTERS = 64_000
MAX_EVIDENCE_CHARACTERS = 300_000
TOOL_TIMEOUT_SECONDS = 30.0
AGENT_MAX_OUTPUT_TOKENS = 64_000


class AgentUnavailableError(RuntimeError):
    """Raised when a beta agent request cannot produce a final answer."""


class CoreAgentService:
    """Let the model choose among fixed tools until it can answer."""

    def __init__(self, model: AgentModel):
        self._model = model

    async def answer(
        self,
        *,
        question: str,
        local_context: str,
        context: AgentRequestContext,
        conversation_history: str = "",
    ) -> str:
        registry = build_agent_tools()
        selection = ToolSelection.for_registry(
            registry, build_agent_tool_groups(),
        )
        context.state.request_text = question
        definitions = (
            selection.definitions() if selection is not None
            else tuple(tool.definition for tool in registry.values())
        )
        compiled = compile_context(
            question=question, local_context=local_context, context=context,
            tools=definitions, conversation_history=conversation_history,
        )
        LOGGER.info(
            "Agent context: request=%s estimated_input_tokens=%s input_bytes=%s omitted_turns=%s exceeds_target=%s estimator=weighted_utf8_v1",
            getattr(context.source_message, "id", None), compiled.estimated_input_tokens,
            compiled.input_content_bytes, compiled.omitted_turns, compiled.exceeds_target,
        )
        session = self._model.create_agent_session(
            system_prompt=SYSTEM_PROMPT,
            prompt=compiled.prompt,
            tools=definitions,
            max_output_tokens=AGENT_MAX_OUTPUT_TOKENS,
        )
        if session is None:
            raise AgentUnavailableError("The AI backend is not configured")

        context_budget = ContextBudget(
            context_window_tokens=getattr(session, "context_window_tokens", None),
            output_reserve=AGENT_MAX_OUTPUT_TOKENS,
            next_input_estimate=compiled.estimated_input_tokens,
        )

        pending_results: tuple[AgentToolResult, ...] = ()
        total_tool_calls = 0
        evidence_characters = 0
        completed_lookups: set[str] = set()
        usage_totals = RequestUsage()
        started_at = time.monotonic()
        status = "incomplete"
        rounds = 0
        final_answer_requested = False
        final_answer_recovery_used = False
        unpublished_snapshot = None

        try:
            # One extra generation is available only to decline calls emitted
            # against the final-answer instruction, never for more research.
            for round_index in range(MAX_MODEL_ROUNDS + 1):
                context = replace(context, member=require_access(
                    context.guild, context.member.id, context.source_message.channel,
                ))
                try:
                    await require_evidence_access(context)
                except AgentAccessLost:
                    if unpublished_snapshot is None:
                        raise
                    pending_results = tuple(await _discard_unpublished_results(
                        context, unpublished_snapshot, pending_results,
                    ))
                projected_input = context_budget.projected_input(pending_results)
                if not context_budget.can_answer(projected_input):
                    status = "context_exhausted"
                    raise AgentUnavailableError("The agent lacks estimated context room for a complete answer")
                # Reserve one full result, not every possible future lookup.
                # Each actual batch is bounded against remaining context below.
                result_reserve = min(
                    MAX_EVIDENCE_CHARACTERS - evidence_characters,
                    MAX_TOOL_RESULT_CHARACTERS,
                ) * 4 + (MAX_TOOL_CALLS - total_tool_calls) * 128
                allow_tools = (
                    not final_answer_requested
                    and round_index < MAX_MODEL_ROUNDS - 1
                    and total_tool_calls < MAX_TOOL_CALLS
                    and evidence_characters < MAX_EVIDENCE_CHARACTERS
                    and context_budget.can_continue_tools(projected_input, result_reserve=result_reserve)
                )
                if not allow_tools:
                    final_answer_requested = True
                    limits = []
                    if round_index >= MAX_MODEL_ROUNDS - 1:
                        limits.append("rounds")
                    if total_tool_calls >= MAX_TOOL_CALLS:
                        limits.append("tool_calls")
                    if evidence_characters >= MAX_EVIDENCE_CHARACTERS:
                        limits.append("evidence")
                    if not context_budget.can_continue_tools(projected_input, result_reserve=result_reserve):
                        limits.append("context")
                    LOGGER.info(
                        "Agent final-answer round: request=%s round=%s limits=%s "
                        "tool_calls=%s evidence_characters=%s projected_input_tokens=%s remaining_context=%s",
                        getattr(context.source_message, "id", None), round_index + 1,
                        ",".join(limits) or "finalization", total_tool_calls, evidence_characters, projected_input,
                        None if context_budget.context_window_tokens is None else context_budget.context_window_tokens - projected_input,
                    )
                usage_totals.attempted_rounds += 1
                round_started_at = time.monotonic()
                try:
                    step = await session.advance(
                        pending_results,
                        allow_tools=allow_tools,
                    )
                except BaseException:
                    LOGGER.info(
                        "Agent model round: request=%s round=%s outcome=failed elapsed_ms=%s",
                        getattr(context.source_message, "id", None),
                        round_index + 1,
                        int((time.monotonic() - round_started_at) * 1_000),
                    )
                    raise
                rounds += 1
                LOGGER.info(
                    "Agent model round: request=%s round=%s outcome=completed "
                    "elapsed_ms=%s provider_duration_ms=%s provider_request_id=%s "
                    "model=%s tool_calls=%s",
                    getattr(context.source_message, "id", None), rounds,
                    int((time.monotonic() - round_started_at) * 1_000),
                    step.provider_duration_ms, step.provider_request_id,
                    step.model_identity, len(step.tool_calls),
                )
                pending_results = ()
                unpublished_snapshot = None
                usage_totals.observe(step.usage)
                context_budget.observe(step.usage, projected_input=projected_input)
                if rounds == 1:
                    LOGGER.info("Agent context calibration: request=%s estimated_input_tokens=%s observed_prompt_tokens=%s",
                                getattr(context.source_message, "id", None), compiled.estimated_input_tokens, step.usage.prompt_tokens)

                if not step.tool_calls:
                    if step.content:
                        status = "completed"
                        return step.content
                    raise AgentUnavailableError("The agent returned no final answer")

                if not allow_tools:
                    if final_answer_recovery_used:
                        raise AgentUnavailableError("The agent did not answer after tool requests were declined")
                    final_answer_recovery_used = True
                    pending_results = tuple(
                        AgentToolResult(call.call_id, _error_result(
                            "This tool call was not executed. Research has ended. "
                            "Give your final answer using the available evidence "
                            "and identify any unanswered parts. Do not call tools again."
                        )) for call in step.tool_calls
                    )
                    continue
                if not context_budget.can_answer(context_budget.next_input_estimate):
                    status = "context_exhausted"
                    raise AgentUnavailableError("The agent lacks estimated context room for another model round")

                results: list[AgentToolResult] = []
                # Nothing in this batch has reached the provider yet. If a
                # newly read source disappears, the whole unpublished batch
                # can be discarded without contaminating the model context.
                unpublished_snapshot = _tool_state_snapshot(context)
                for call_index, call in enumerate(step.tool_calls):
                    if total_tool_calls >= MAX_TOOL_CALLS:
                        results.append(
                            AgentToolResult(
                                call_id=call.call_id,
                                content=_error_result(
                                    "The request has reached its tool-call limit. "
                                    "Answer from the evidence already collected."
                                ),
                            )
                        )
                        continue
                    total_tool_calls += 1
                    if selection is not None and call.name == DISCOVERY_TOOL_NAME:
                        arguments = _parse_arguments(call.arguments)
                        discovery = selection.definitions()[0]
                        if (
                            arguments is None
                            or not _valid_arguments(arguments, discovery.parameters)
                        ):
                            results.append(AgentToolResult(
                                call_id=call.call_id,
                                content=_error_result(
                                    "Arguments must match the tool's declared fields and types."
                                ),
                            ))
                            continue
                        previous_definitions = definitions
                        payload = selection.activate(arguments["groups"])
                        definitions = selection.definitions()
                        if "error" not in payload:
                            session.replace_tools(definitions)
                            definition_delta = (
                                estimate_tokens(encoded_definitions(definitions))
                                - estimate_tokens(encoded_definitions(
                                    previous_definitions,
                                ))
                            )
                            context_budget.next_input_estimate = max(
                                0, context_budget.next_input_estimate
                                + definition_delta,
                            )
                        results.append(AgentToolResult(
                            call_id=call.call_id,
                            content=json.dumps(payload, ensure_ascii=False),
                        ))
                        continue
                    registered = registry.get(call.name)
                    if (
                        registered is None
                        or selection is not None
                        and call.name not in selection.active_names
                    ):
                        results.append(
                            AgentToolResult(
                                call_id=call.call_id,
                                content=_error_result("That tool is not available."),
                            )
                        )
                        continue
                    arguments = _parse_arguments(call.arguments)
                    if arguments is None or not _valid_arguments(arguments, registered.definition.parameters):
                        results.append(
                            AgentToolResult(
                                call_id=call.call_id,
                                content=_error_result("Arguments must match the tool's declared fields and types."),
                            )
                        )
                        continue
                    if evidence_characters >= MAX_EVIDENCE_CHARACTERS:
                        results.append(
                            AgentToolResult(
                                call_id=call.call_id,
                                content=_error_result(
                                    "The request has reached its evidence limit. "
                                    "Answer from the evidence already collected."
                                ),
                            )
                        )
                        continue
                    result_limit = context_budget.result_character_limit(
                        results,
                        pending_call_ids=tuple(item.call_id for item in step.tool_calls[call_index:]),
                        maximum=min(MAX_TOOL_RESULT_CHARACTERS, MAX_EVIDENCE_CHARACTERS - evidence_characters),
                    )
                    if result_limit < 512:
                        final_answer_requested = True
                        results.append(AgentToolResult(call.call_id, _error_result(
                            "This lookup was not executed because the remaining context "
                            "is reserved for your answer. Answer from existing evidence."
                        )))
                        continue
                    context = replace(context, member=require_access(
                        context.guild, context.member.id, context.source_message.channel,
                    ))
                    try:
                        await require_evidence_access(context)
                    except AgentAccessLost:
                        results = await _discard_unpublished_results(
                            context, unpublished_snapshot, results,
                        )
                    cache_key = call.name + json.dumps(arguments, sort_keys=True)
                    if cache_key in completed_lookups:
                        results.append(AgentToolResult(call.call_id, _error_result(
                            "This identical lookup already ran. Use its earlier result."
                        )))
                        continue
                    try:
                        content = await self._execute_tool(
                            name=call.name,
                            handler=registered.handler,
                            arguments=arguments,
                            context=context,
                        )
                    except AgentAccessLost:
                        results = await _discard_unpublished_results(
                            context, unpublished_snapshot, results,
                        )
                        completed_lookups.add(cache_key)
                        results.append(AgentToolResult(call.call_id, _error_result("That lookup failed.")))
                        continue
                    content = _bound_tool_result(
                        content,
                        result_limit,
                    )
                    evidence_characters += len(content)
                    context.state.evidence.append(json.dumps({"tool": call.name, "arguments": arguments, "result": content}, ensure_ascii=False))
                    completed_lookups.add(cache_key)
                    results.append(
                        AgentToolResult(call_id=call.call_id, content=content)
                    )
                pending_results = tuple(results)
        except TextGenerationError as error:
            status = "provider_error"
            raise AgentUnavailableError(str(error)) from error
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except AgentAccessLost:
            status = "access_lost"
            raise
        finally:
            self._log_completion(
                context=context, rounds=rounds, tool_calls=total_tool_calls,
                evidence_characters=evidence_characters, usage=usage_totals,
                status=status, elapsed_ms=int((time.monotonic() - started_at) * 1000),
            )

        raise AgentUnavailableError("The agent did not finish")

    @staticmethod
    async def _execute_tool(
        *,
        name: str,
        handler: Any,
        arguments: Mapping[str, Any],
        context: AgentRequestContext,
    ) -> str:
        snapshot = _tool_state_snapshot(context)
        started_at = time.monotonic()
        outcome = "failed"
        try:
            async with asyncio.timeout(TOOL_TIMEOUT_SECONDS):
                payload = await handler(context, arguments)
            _record_report_provenance(context, snapshot["reports"])
            await require_evidence_access(context)
            outcome = "completed"
            return json.dumps(payload, ensure_ascii=False, default=str)
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
                "Agent tool: tool=%s invoker=%s outcome=%s elapsed_ms=%s",
                name, context.member.id, outcome,
                int((time.monotonic() - started_at) * 1_000),
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


def _tool_state_snapshot(context: AgentRequestContext) -> dict[str, Any]:
    """Capture request-local state that a failed tool must not partially mutate."""
    state = context.state
    return {
        "evidence": list(state.evidence),
        "source_channels": set(state.source_channels),
        "reports": dict(state.reports),
        "report_sources": dict(state.report_sources),
        "report_access_requirements": dict(state.report_access_requirements),
        "required_access": set(state.required_access),
        "attachments": list(state.attachments),
        "history_status": dict(state.history_status),
        "authorized_history": state.authorized_history,
        "authorized_checkpoint": state.authorized_checkpoint,
        "working": state.working,
        "authorized_instructions": state.authorized_instructions,
    }


def _restore_tool_state(
    context: AgentRequestContext, snapshot: Mapping[str, Any],
) -> None:
    """Restore the exact request-local state after an unsuccessful tool call."""
    state = context.state
    state.evidence[:] = snapshot["evidence"]
    state.source_channels.clear()
    state.source_channels.update(snapshot["source_channels"])
    state.reports.clear()
    state.reports.update(snapshot["reports"])
    state.report_sources.clear()
    state.report_sources.update(snapshot["report_sources"])
    state.report_access_requirements.clear()
    state.report_access_requirements.update(
        snapshot["report_access_requirements"]
    )
    state.required_access.clear()
    state.required_access.update(snapshot["required_access"])
    state.attachments[:] = snapshot["attachments"]
    state.history_status.clear()
    state.history_status.update(snapshot["history_status"])
    state.authorized_history = snapshot["authorized_history"]
    state.authorized_checkpoint = snapshot["authorized_checkpoint"]
    state.working = snapshot["working"]
    state.authorized_instructions = snapshot["authorized_instructions"]


async def _discard_unpublished_results(
    context: AgentRequestContext,
    snapshot: Mapping[str, Any],
    results: Sequence[AgentToolResult],
) -> list[AgentToolResult]:
    """Discard unsent evidence; never continue with revoked model context."""
    _restore_tool_state(context, snapshot)
    # This includes Core membership, the request channel, earlier evidence and
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


def _parse_arguments(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _valid_arguments(arguments: Mapping[str, Any], schema: Mapping[str, Any]) -> bool:
    """Validate the bounded schemas exposed by the agent catalogue."""
    properties = schema.get("properties", {})
    if set(arguments) - set(properties) or set(schema.get("required", ())) - set(arguments):
        return False
    for name, value in arguments.items():
        field = properties[name]
        if "enum" in field and value not in field["enum"]:
            return False
        if field.get("type") == "string":
            if not isinstance(value, str) or not value.strip():
                return False
            if not field.get("minLength", 1) <= len(value) <= field.get("maxLength", 1024):
                return False
        elif field.get("type") == "integer":
            if type(value) is not int:
                return False
            if not field.get("minimum", 1) <= value <= field.get("maximum", 2**64 - 1):
                return False
        elif field.get("type") == "boolean":
            if type(value) is not bool:
                return False
        elif field.get("type") == "array":
            if not isinstance(value, list) or not field.get("minItems", 0) <= len(value) <= field.get("maxItems", 20):
                return False
            if any(not _valid_arguments({"item": item}, {"properties": {"item": field["items"]}}) for item in value):
                return False
            if field.get("uniqueItems") and len(set(value)) != len(value):
                return False
        elif field.get("type") == "object":
            if not isinstance(value, Mapping) or not _valid_arguments(value, field):
                return False
        else:
            return False
    return True


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
