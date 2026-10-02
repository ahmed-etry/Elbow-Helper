"""Bounded model rounds and response continuations."""

from __future__ import annotations

import logging

import asyncio
from dataclasses import replace
import json
import time
from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort
from elbow_helper.infrastructure.ai import AgentToolResult
from ..access import AgentAccessLost
from ..disclosure import require_disclosure_access
from ..conversation.context import estimate_tokens
from ..wording import AGENT_ANSWER_UNFINISHED, AGENT_RESEARCH_UNFINISHED
from . import budgets as limits
from .tool_call import discard_unpublished_results
from ..models import AgentRequestContext
from .budgets import ContextBudget
from .usage import RequestUsage
from ..plan.scope import ScopeLedger
from elbow_helper.infrastructure.ai.agent import AgentSession

LOGGER = logging.getLogger(__name__)


class AgentGracefulEnd(RuntimeError):
    """Return a bounded response when planning or answering cannot continue."""


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
        ledger: ScopeLedger,
    ) -> None:
        self.context = context
        self.budget = budget
        self.usage = usage
        self.session = session
        self.request_id = request_id
        self.ledger = ledger
        self.rounds = 0
        self.unpublished = None
        self.unpublished_scope = None

    async def advance(
        self,
        results=(),
        *,
        allow_tools=True,
        reasoning_effort=AgentReasoningEffort.LOW,
        max_output_tokens=limits.INITIAL_MAX_OUTPUT_TOKENS,
        continuation_instruction=None,
        continued=False,
    ):
        try:
            await require_disclosure_access(self.context)
        except AgentAccessLost:
            if self.unpublished is None:
                raise
            await discard_unpublished_results(self.context, self.unpublished, results)
            self.ledger.reports = self.unpublished_scope
            results = tuple(
                (
                    AgentToolResult(
                        item.call_id,
                        json.dumps(
                            {
                                "flags": {"status": "failed", "limits": ["access_lost"]},
                                "instruction": "Answer using the remaining authorized context.",
                            }
                        ),
                    )
                    for item in results
                )
            )
        self.unpublished = self.unpublished_scope = None
        projected = self.budget.projected_input(results) + estimate_tokens(
            continuation_instruction or ""
        )
        remaining = (
            max_output_tokens
            if self.budget.context_window_tokens is None
            else min(max_output_tokens, self.budget.context_window_tokens - projected)
        )
        if remaining < limits.MIN_FINAL_OUTPUT_TOKENS:
            raise AgentGracefulEnd(AGENT_RESEARCH_UNFINISHED)
        if self.rounds >= limits.MAX_MODEL_ROUNDS + 1:
            raise AgentGracefulEnd(AGENT_RESEARCH_UNFINISHED)
        self.budget.output_reserve = remaining
        self.usage.attempted_rounds += 1
        self.rounds += 1
        round_started = time.monotonic()
        outcome = "failed"
        try:
            async with asyncio.timeout_at(self.context.deadline_monotonic):
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
            self.usage.observe(model_step.usage)
            self.budget.observe(model_step.usage, projected_input=projected)
            outcome = "completed"
            LOGGER.info(
                (
                    (
                        'Agent model response: request=%s round=%s '
                        'model=%s provider_request_id=%s '
                        'provider_duration_ms=%s prompt_tokens=%s '
                        'completion_tokens=%s cache_hit_tokens=%s '
                        'cache_miss_tokens=%s'
                    )
                ),
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
                LOGGER.warning(
                    "Agent model output limit reached: request=%s round=%s limit=%s",
                    self.request_id,
                    self.rounds,
                    remaining,
                )
                if continued:
                    raise AgentGracefulEnd(AGENT_ANSWER_UNFINISHED)
                incomplete_calls = tuple(
                    (
                        AgentToolResult(
                            call.call_id,
                            json.dumps(
                                {
                                    "error": (
                                        (
                                            'The prior model output was incomplete. '
                                            'Submit the full plan again.'
                                        )
                                    )
                                }
                            ),
                        )
                        for call in model_step.tool_calls
                    )
                )
                instruction = (
                    "Submit the full tool call again; the previous one was incomplete."
                    if incomplete_calls
                    else (
                        (
                            (
                                'Continue the previous answer from where it '
                                'stopped. Do not repeat it.'
                            )
                        )
                    )
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
        finally:
            LOGGER.info(
                (
                    (
                        'Agent model round: request=%s round=%s '
                        'outcome=%s effort=%s output_limit=%s '
                        'elapsed_ms=%s'
                    )
                ),
                self.request_id,
                self.rounds,
                outcome,
                reasoning_effort.value,
                remaining,
                int((time.monotonic() - round_started) * 1000),
            )
