"""Bounded model rounds and response continuations."""

from __future__ import annotations

import logging

import asyncio
from dataclasses import asdict, replace
import json
import time
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from elbow_helper.infrastructure.ai import AgentToolResult, AgentUsage
from ..access import AgentAccessLost, require_evidence_access
from ..conversation.context import estimate_tokens
from ..wording import AGENT_ANSWER_UNFINISHED, AGENT_RESEARCH_UNFINISHED
from . import budgets as limits
from .tool_call import discard_unpublished_results
from ..models import AgentRequestContext
from .budgets import ContextBudget
from .usage import RequestUsage
from .routing import EXPAND_CONTEXT
from .self_knowledge import ability_claim, capability_question
from elbow_helper.infrastructure.ai.agent import AgentSession
from ..prompts import (
    REPEAT_TOOL_CALL_INSTRUCTION,
    CONTINUE_ANSWER_INSTRUCTION,
    AUTHORIZED_CONTEXT_INSTRUCTION,
    INCOMPLETE_PLAN_INSTRUCTION,
    LIMIT_ANSWER_INSTRUCTION,
)

LOGGER = logging.getLogger(__name__)

SHORT_REPLY_REWRITE_INSTRUCTION = (
    "Discard the unfinished draft. Write a fresh, complete reply in one short line using "
    "the required reply format. Do not continue the fragment or discuss generation limits. "
    "If community facts or work are needed, call load_research_context alone when offered."
)


class AgentGracefulEnd(RuntimeError):
    """Return a bounded response when planning or answering cannot continue."""


class AgentLimitReached(AgentGracefulEnd):
    """A request limit stopped further planning or answering."""


class ModelRounds:
    """Advance bounded model rounds and retain unpublished result state."""

    def __init__(
        self,
        *,
        context: AgentRequestContext,
        budget: ContextBudget,
        usage: RequestUsage,
        session: AgentSession,
        request_id: int | None,
    ) -> None:
        self.context = context
        self.budget = budget
        self.usage = usage
        self.session = session
        self.request_id = request_id
        self.rounds = 0
        self.unpublished = None
        self.pending_results = ()
        self.pending_calls = ()
        self.final_attempted = False
        self.results_available = False
        self.social_route = False
        self.game_advice_route = False
        self.expand_context = None
        self.last_allow_tools = True
        self.budget_limited = False
        self.request_effort = AgentReasoningEffort.HIGH
        self.partial_content = ""
        self.capability_context = None
        self.capability_session = None
        self.capabilities_checked = False

    async def advance(self, results=(), **kwargs):
        if (self.capability_context is not None and self.capability_session is not self.session
                and (self.capabilities_checked or not self.social_route
                     and capability_question(self.context.state.request_text))):
            # Isolated writers do not inherit the planning transcript. Supply
            # the index again after a session change, with fresh access facts.
            await require_evidence_access(self.context)
            kwargs["continuation_instruction"] = "\n\n".join(filter(None, (
                kwargs.get("continuation_instruction"), self.capability_context(),
            )))
            self.capability_session = self.session
            self.capabilities_checked = True
        step = await self._advance(results, **kwargs)
        if step.tool_calls and not self.last_allow_tools and self.budget_limited:
            raise AgentLimitReached(AGENT_RESEARCH_UNFINISHED)
        if (len(step.tool_calls) == 1
                and step.tool_calls[0].name == EXPAND_CONTEXT
                and self.expand_context is not None
                and self.last_allow_tools):
            call = step.tool_calls[0]
            try:
                valid = json.loads(call.arguments) == {}
            except (TypeError, ValueError):
                valid = False
            if valid:
                await require_evidence_access(self.context)
                instructions = await self.expand_context()
                self.expand_context = None
                return await self.advance(
                    (AgentToolResult(call.call_id, instructions),),
                    **{**kwargs, "reasoning_effort": AgentReasoningEffort.HIGH},
                )
        if (not self.social_route and self.capability_context is not None
                and self.capability_session is not self.session
                and ability_claim(step, self.context.state.request_text,
                                  has_results=self.results_available)):
            # Read only for a proposed self-description. Routine lookups and
            # banter keep their original prompts, tools and model round count.
            await require_evidence_access(self.context)
            instruction = ("The previous draft was not delivered or executed. Rewrite it using "
                           "the complete index below; resubmit any needed plan.\n"
                           + self.capability_context())
            self.capability_session = self.session
            self.capabilities_checked = True
            pending = tuple(AgentToolResult(call.call_id, json.dumps({
                "error": "Draft not executed. Consult the complete capability index before replying.",
            })) for call in step.tool_calls)
            return await self.advance(pending, **{**kwargs,
                "final": False,
                "continuation_instruction": "\n\n".join(filter(None, (
                    kwargs.get("continuation_instruction"), instruction,
                ))),
            })
        return step

    async def _advance(
        self,
        results=(),
        *,
        allow_tools=True,
        reasoning_effort=None,
        max_output_tokens=limits.INITIAL_MAX_OUTPUT_TOKENS,
        continuation_instruction=None,
        continued=False,
        final=False,
    ):
        # Planning, continuations and repairs share a floor. A plan can ask
        # for more reasoning, but cannot downgrade a later answer or repair.
        reasoning_effort = max(
            (self.request_effort, reasoning_effort or self.request_effort),
            key=list(AgentReasoningEffort).index,
        )
        self.request_effort = reasoning_effort
        self.pending_results = tuple(results)
        try:
            await require_evidence_access(self.context)
        except AgentAccessLost:
            if self.unpublished is None:
                raise
            await discard_unpublished_results(self.context, self.unpublished, results)
            results = tuple(
                (
                    AgentToolResult(
                        item.call_id,
                        json.dumps(
                            {
                                "flags": {"status": "failed", "limits": ["access_lost"]},
                                "instruction": AUTHORIZED_CONTEXT_INSTRUCTION,
                            }
                        ),
                    )
                    for item in results
                )
            )
        self.unpublished = None
        self.pending_results = tuple(results)
        projected = self.budget.projected_input(results) + estimate_tokens(
            continuation_instruction or ""
        )
        if allow_tools and not self.budget.request_can_research(projected):
            if self.budget.request_limits and self.budget.request_limits.final_output_tokens:
                # Assessment reviews contain pages, not the final tables or
                # decision protocol. Let AnswerFlow prepare its full final;
                # never silently bill this review as a short answer instead.
                self.budget_limited = True
                raise AgentLimitReached(AGENT_RESEARCH_UNFINISHED)
            allow_tools = False
            self.budget_limited = True
            continuation_instruction = "\n".join(filter(None, (
                continuation_instruction, LIMIT_ANSWER_INSTRUCTION,
            )))
            projected += estimate_tokens(LIMIT_ANSWER_INSTRUCTION)
        self.last_allow_tools = allow_tools
        request_output = self.budget.request_output_limit(
            projected, allow_tools=allow_tools, final=final,
        )
        max_output_tokens = min(max_output_tokens, request_output)
        remaining = (
            max_output_tokens
            if self.budget.context_window_tokens is None
            else min(max_output_tokens, self.budget.context_window_tokens - projected)
        )
        if remaining < (1 if final else limits.MIN_FINAL_OUTPUT_TOKENS):
            if request_output < (1 if final else limits.MIN_FINAL_OUTPUT_TOKENS):
                self.budget_limited = True
            raise AgentLimitReached(AGENT_RESEARCH_UNFINISHED)
        if final:
            if self.final_attempted:
                raise AgentLimitReached(AGENT_RESEARCH_UNFINISHED)
            self.final_attempted = True
        elif self.rounds >= limits.MAX_MODEL_ROUNDS + 1:
            raise AgentLimitReached(AGENT_RESEARCH_UNFINISHED)
        self.budget.output_reserve = remaining
        self.usage.attempted_rounds += 1
        self.rounds += 1
        round_number = self.rounds
        round_started = time.monotonic()
        model_step = None
        round_elapsed_ms = None
        outcome = "failed"
        observed = False
        try:
            self.pending_results = self.pending_calls = ()
            deadline = self.context.deadline_monotonic
            if deadline is not None:
                deadline -= limits.DELIVERY_TIME_RESERVE_SECONDS
                if allow_tools:
                    deadline -= limits.FINAL_ANSWER_TIME_RESERVE_SECONDS
            async with asyncio.timeout_at(deadline):
                model_step = await self.session.advance(
                    results,
                    allow_tools=allow_tools,
                    reasoning_effort=reasoning_effort,
                    max_output_tokens=remaining,
                    **(
                        {"continuation_instruction": continuation_instruction}
                        if continuation_instruction
                        else {}
                    ),
                )
            round_elapsed_ms = int((time.monotonic() - round_started) * 1000)
            debug = getattr(self.context, "operator_debug", None)
            if debug is not None:
                debug.submitted(model_step.tool_calls)
            self.pending_calls = model_step.tool_calls
            self.usage.observe(model_step.usage)
            self.budget.observe(model_step.usage, projected_input=projected)
            observed = True
            outcome = "completed"
            LOGGER.info(
                ("Agent model response: request=%s round=%s "
                            "model=%s provider_request_id=%s "
                            "provider_duration_ms=%s prompt_tokens=%s "
                            "completion_tokens=%s cache_hit_tokens=%s "
                            "cache_miss_tokens=%s"),
                self.request_id,
                self.rounds,
                model_step.model_identity,
                model_step.provider_request_id,
                model_step.provider_duration_ms,
                model_step.usage.prompt_tokens,
                model_step.usage.completion_tokens,
                model_step.usage.prompt_cache_hit_tokens,
                model_step.usage.prompt_cache_miss_tokens,
            )
            if model_step.output_limit_reached:
                if self.social_route or self.game_advice_route:
                    # Short replies must be complete, never a fragment followed by
                    # the research writer's unfinished-answer notice. One rewrite
                    # shares the existing request budget and deadline.
                    self.partial_content = ""
                    if continued or final:
                        raise AgentLimitReached(AGENT_ANSWER_UNFINISHED)
                    pending = tuple(AgentToolResult(call.call_id, json.dumps({
                        "error": SHORT_REPLY_REWRITE_INSTRUCTION,
                    })) for call in model_step.tool_calls)
                    return await self.advance(
                        pending, allow_tools=allow_tools, reasoning_effort=reasoning_effort,
                        max_output_tokens=1024, continuation_instruction=SHORT_REPLY_REWRITE_INSTRUCTION,
                        continued=True,
                    )
                if model_step.content and not model_step.tool_calls:
                    self.partial_content = (
                        self.partial_content + "\n" + model_step.content
                        if continued and self.partial_content else model_step.content
                    )
                LOGGER.warning(
                    "Agent model output limit reached: request=%s round=%s limit=%s",
                    self.request_id,
                    self.rounds,
                    remaining,
                )
                if continued or final or self.results_available:
                    if continued or final:
                        self.final_attempted = True
                    raise AgentLimitReached(AGENT_ANSWER_UNFINISHED)
                incomplete_calls = tuple(
                    (
                        AgentToolResult(
                            call.call_id,
                            json.dumps({"error": INCOMPLETE_PLAN_INSTRUCTION}),
                        )
                        for call in model_step.tool_calls
                    )
                )
                instruction = (
                    REPEAT_TOOL_CALL_INSTRUCTION
                    if incomplete_calls
                    else CONTINUE_ANSWER_INSTRUCTION
                )
                continuation = await self.advance(
                    incomplete_calls,
                    allow_tools=allow_tools,
                    reasoning_effort=reasoning_effort,
                    max_output_tokens=max_output_tokens,
                    continuation_instruction=instruction,
                    continued=True,
                )
                if incomplete_calls or continuation.tool_calls:
                    return continuation
                return replace(
                    continuation,
                    content=(
                        model_step.content + "\n" + continuation.content
                        if model_step.content
                        else continuation.content
                    ),
                )
            return model_step
        except TimeoutError as error:
            raise AgentLimitReached(AGENT_RESEARCH_UNFINISHED) from error
        finally:
            if not observed:
                # A failed or cancelled attempt may still be billed by the provider.
                self.budget.observe(AgentUsage(), projected_input=projected)
            debug = getattr(self.context, "operator_debug", None)
            if debug is not None:
                debug.rounds.append({
                    "round": round_number, "effort": reasoning_effort.value,
                    "outcome": outcome, "output_limit": remaining,
                    "elapsed_ms": round_elapsed_ms if round_elapsed_ms is not None else
                    int((time.monotonic() - round_started) * 1000),
                    "usage": asdict(model_step.usage) if model_step is not None else None,
                    "content": model_step.content if model_step is not None else None,
                })
            LOGGER.info(
                ("Agent model round: request=%s round=%s "
                            "outcome=%s effort=%s output_limit=%s "
                            "elapsed_ms=%s"),
                self.request_id,
                self.rounds,
                outcome,
                reasoning_effort.value,
                remaining,
                int((time.monotonic() - round_started) * 1000),
            )
