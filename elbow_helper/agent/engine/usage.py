"""Request-local observed usage, without pricing or billing assumptions."""

from dataclasses import dataclass

from elbow_helper.infrastructure.ai import AgentUsage


@dataclass(slots=True)
class RequestUsage:
    attempted_rounds: int = 0
    reported_rounds: int = 0
    incomplete_token_rounds: int = 0
    incomplete_cache_rounds: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0

    def observe(self, usage: AgentUsage) -> None:
        self.reported_rounds += 1
        self.incomplete_token_rounds += int(
            usage.prompt_tokens is None or usage.completion_tokens is None
        )
        self.incomplete_cache_rounds += int(
            usage.prompt_cache_hit_tokens is None or usage.prompt_cache_miss_tokens is None
        )
        self.prompt_tokens += usage.prompt_tokens or 0
        self.completion_tokens += usage.completion_tokens or 0
        self.cache_hit_tokens += usage.prompt_cache_hit_tokens or 0
        self.cache_miss_tokens += usage.prompt_cache_miss_tokens or 0

    @property
    def unknown_token_rounds(self) -> int:
        return self.attempted_rounds - self.reported_rounds + self.incomplete_token_rounds

    @property
    def unknown_cache_rounds(self) -> int:
        return self.attempted_rounds - self.reported_rounds + self.incomplete_cache_rounds
