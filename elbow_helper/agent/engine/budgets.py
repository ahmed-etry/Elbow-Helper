"""Context headroom and cumulative request spending, including recovery rounds."""

from elbow_helper.infrastructure.ai.agent import AgentReasoningEffort

from dataclasses import dataclass
from typing import Sequence

from elbow_helper.infrastructure.ai import AgentToolResult, AgentUsage

from ..conversation.context import estimate_tokens


@dataclass(frozen=True, slots=True)
class RequestLimits:
    rounds: int
    input_tokens: int
    output_tokens: int
    round_output_tokens: int
    answer_repair_rounds: int = 0
    final_output_tokens: int | None = None


# Routing, initial plan, dependent read, synthesis and one answer repair.
FAMILY_LIMITS = RequestLimits(5, 180_000, 20_000, 8_192, answer_repair_rounds=1)
LOOKUP_LIMITS = RequestLimits(4, 80_000, 12_000, 4_096)
# Banter resends a meme catalogue of about 19k tokens each round, mostly as a
# cached prefix; leave room for a member-identity round and a text-only repair.
SOCIAL_LIMITS = RequestLimits(4, 160_000, 12_000, 4_096)
# The research prefix alone is about 26k tokens. Bound dependent batches and
# reserve a compact synthesis instead of repeatedly resending the catalogue.
RESEARCH_LIMITS = RequestLimits(10, 240_000, 32_000, 8_192)
# Max-effort assessments share one request allowance, including a short repair.
# Reasoning and visible text both consume the provider's completion limit.
# Complete authorized evidence can exceed 120k input tokens. Allow compact
# dependent research plus final/recovery inputs, without resending full tables
# for each research decision. A captured large assessment needed 40k output
# tokens to finish; 32k interrupted its visible answer and forced a second full
# input. Reserve visible composition room as well as one bounded repair.
ASSESSMENT_LIMITS = RequestLimits(10, 400_000, 65_536, 8_192,
                                answer_repair_rounds=1, final_output_tokens=49_152)
REQUEST_FINAL_RESERVE = 2_048


@dataclass(slots=True)
class ContextBudget:
    context_window_tokens: int | None
    output_reserve: int
    next_input_estimate: int
    final_answer_reserve: int | None = None
    request_limits: RequestLimits | None = None
    spent_input: int = 0
    spent_output: int = 0
    spent_rounds: int = 0
    # A compact research page is not the size of the full final evidence.
    final_input_estimate: int | None = None

    @property
    def request_final_reserve(self) -> int:
        policy = self.request_limits
        if policy and policy.final_output_tokens:
            return policy.final_output_tokens + policy.answer_repair_rounds * policy.round_output_tokens
        return max(REQUEST_FINAL_RESERVE, policy.round_output_tokens) if policy else REQUEST_FINAL_RESERVE

    def request_output_limit(self, projected_input: int, *, allow_tools: bool, final: bool = False) -> int:
        policy = self.request_limits
        if policy is None:
            return AGENT_MAX_OUTPUT_TOKENS
        if (self.spent_rounds >= policy.rounds
                or self.spent_input + projected_input > policy.input_tokens):
            return 0
        repair_reserve = (policy.answer_repair_rounds * policy.round_output_tokens
                          if final and policy.final_output_tokens else 0)
        return max(0, min(
            (policy.final_output_tokens or FINAL_MAX_OUTPUT_TOKENS) if final else policy.round_output_tokens,
            policy.output_tokens - self.spent_output
            - (self.request_final_reserve if allow_tools else repair_reserve),
        ))

    def request_can_research(self, projected_input: int) -> bool:
        policy = self.request_limits
        final_input = projected_input
        final_rounds = 1
        if policy and policy.final_output_tokens:
            final_input = (self.final_input_estimate if self.final_input_estimate is not None
                           else projected_input)
            final_rounds += policy.answer_repair_rounds
        return policy is None or (
            self.spent_rounds < policy.rounds - 1 - policy.answer_repair_rounds
            and self.spent_input + projected_input + final_rounds * final_input + 8192 <= policy.input_tokens
            and policy.output_tokens - self.spent_output
            >= self.request_final_reserve + MIN_FINAL_OUTPUT_TOKENS
        )

    @property
    def final_reserve(self) -> int:
        return (
            self.output_reserve if self.final_answer_reserve is None else self.final_answer_reserve
        )

    def projected_input(self, results: Sequence[AgentToolResult]) -> int:
        return self.next_input_estimate + sum(
            estimate_tokens(result.content) + estimate_tokens(result.call_id) + 128
            for result in results
        )

    def can_answer(self, projected_input: int) -> bool:
        return (
            self.context_window_tokens is None
            or projected_input + self.output_reserve <= self.context_window_tokens
        )

    def can_continue_tools(self, projected_input: int, *, result_reserve: int) -> bool:
        # Reserve this round, one bounded result, then a final answer.
        policy = self.request_limits
        request_room = policy is None or (
            self.spent_rounds < policy.rounds
            and self.spent_input + projected_input + result_reserve <= policy.input_tokens
            and policy.output_tokens - self.spent_output >= self.request_final_reserve
        )
        return request_room and (
            self.context_window_tokens is None
            or projected_input + self.output_reserve + self.final_reserve + result_reserve
            <= self.context_window_tokens
        )

    def observe(self, usage: AgentUsage, *, projected_input: int) -> None:
        # Completion usage includes continuation text/reasoning held by the
        # provider adapter. Missing usage is not zero: reserve its full allowance.
        prompt = usage.prompt_tokens if usage.prompt_tokens is not None else projected_input
        completion = (
            usage.completion_tokens if usage.completion_tokens is not None else self.output_reserve
        )
        self.spent_rounds += 1
        self.spent_input += prompt
        self.spent_output += completion
        self.next_input_estimate = prompt + completion + 128

    def result_character_limit(
        self,
        results: Sequence[AgentToolResult],
        *,
        pending_call_ids: Sequence[str],
        maximum: int,
    ) -> int:
        """Fit the next result while retaining answer and remaining reply room."""
        if self.context_window_tokens is None:
            return maximum
        # Reserve final instructions and a small refusal for every outstanding
        # call. Four UTF-8 bytes per character bounds the next result's tokens.
        framing = 1024 + sum(estimate_tokens(call_id) + 640 for call_id in pending_call_ids)
        available = (
            self.context_window_tokens
            - self.final_reserve
            - self.projected_input(results)
            - framing
        )
        return min(maximum, max(0, available // 4))


MAX_MODEL_ROUNDS = 20
# A safeguard against runaway plans, not a reading budget: the evidence
# budgets fill after roughly thirty useful reads, and the rest leaves room
# for argument repairs. Raise it together with those budgets.
MAX_TOOL_CALLS = 40
MAX_TOOL_RESULT_CHARACTERS = 16_000
MAX_EVIDENCE_CHARACTERS = 64_000
MAX_DISCUSSION_EVIDENCE_CHARACTERS = 128_000
TOOL_TIMEOUT_SECONDS = 30.0
COMMAND_TIMEOUT_SECONDS = 180.0
MODEL_ROUND_TIME_RESERVE_SECONDS = 15.0
FINAL_ANSWER_TIME_RESERVE_SECONDS = 45.0
DELIVERY_TIME_RESERVE_SECONDS = 15.0
AGENT_MAX_OUTPUT_TOKENS = 64_000
INITIAL_MAX_OUTPUT_TOKENS = 32_000
FINAL_MAX_OUTPUT_TOKENS = 16_000
ANSWER_OUTPUT_LIMITS = {
    AgentReasoningEffort.NONE: FINAL_MAX_OUTPUT_TOKENS,
    AgentReasoningEffort.LOW: FINAL_MAX_OUTPUT_TOKENS,
    AgentReasoningEffort.HIGH: 32_000,
    AgentReasoningEffort.MAX: 64_000,
}
MIN_FINAL_OUTPUT_TOKENS = 1_024
