"""Model-context headroom across one tool-assisted request, not a money quota."""

from dataclasses import dataclass
from typing import Sequence

from elbow_helper.infrastructure.ai import AgentToolResult, AgentUsage

from .conversation.context import estimate_tokens


@dataclass(slots=True)
class ContextBudget:
    context_window_tokens: int | None
    output_reserve: int
    next_input_estimate: int

    def projected_input(self, results: Sequence[AgentToolResult]) -> int:
        return self.next_input_estimate + sum(
            estimate_tokens(result.content) + estimate_tokens(result.call_id) + 128
            for result in results
        )

    def can_answer(self, projected_input: int) -> bool:
        return self.context_window_tokens is None or projected_input + self.output_reserve <= self.context_window_tokens

    def can_continue_tools(self, projected_input: int, *, result_reserve: int) -> bool:
        # Reserve one tool-calling output, bounded tool evidence, then a final
        # answer. The reserve does not reduce the model's output allowance.
        return (self.context_window_tokens is None
                or projected_input + 2 * self.output_reserve + result_reserve <= self.context_window_tokens)

    def observe(self, usage: AgentUsage, *, projected_input: int) -> None:
        # Completion usage includes continuation text/reasoning held by the
        # provider adapter. Missing usage is not zero: reserve its full allowance.
        prompt = usage.prompt_tokens if usage.prompt_tokens is not None else projected_input
        completion = usage.completion_tokens if usage.completion_tokens is not None else self.output_reserve
        self.next_input_estimate = prompt + completion + 128

    def result_character_limit(
        self, results: Sequence[AgentToolResult], *, pending_call_ids: Sequence[str],
        maximum: int,
    ) -> int:
        """Fit the next result while retaining answer and remaining reply room."""
        if self.context_window_tokens is None:
            return maximum
        # Reserve final instructions and a small refusal for every outstanding
        # call. Four UTF-8 bytes per character bounds the next result's tokens.
        framing = 1024 + sum(estimate_tokens(call_id) + 640 for call_id in pending_call_ids)
        available = (
            self.context_window_tokens - self.output_reserve
            - self.projected_input(results) - framing
        )
        return min(maximum, max(0, available // 4))
