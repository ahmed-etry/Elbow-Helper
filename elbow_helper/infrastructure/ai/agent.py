"""Provider-neutral contracts for multi-round tool-assisted generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from typing import Mapping
from typing import Protocol
from typing import Sequence


@dataclass(frozen=True, slots=True)
class AgentToolDefinition:
    """A callable capability described to a text-generation provider."""

    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AgentToolCall:
    """One tool invocation requested by the model."""

    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class AgentToolResult:
    """Result returned to the model for one requested tool invocation."""

    call_id: str
    content: str


@dataclass(frozen=True, slots=True)
class AgentUsage:
    """Provider-reported token usage for one model round."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_cache_hit_tokens: int = 0
    prompt_cache_miss_tokens: int = 0


@dataclass(frozen=True, slots=True)
class AgentStep:
    """One model response, either requesting tools or completing the answer."""

    content: str
    tool_calls: tuple[AgentToolCall, ...]
    usage: AgentUsage


class AgentSession(Protocol):
    """A provider-owned conversation that preserves tool-call continuation state."""

    async def advance(
        self,
        tool_results: Sequence[AgentToolResult] = (),
        *,
        allow_tools: bool = True,
    ) -> AgentStep: ...


class AgentModel(Protocol):
    """Contract consumed by features that need multi-round tool use."""

    @property
    def configured(self) -> bool: ...

    def create_agent_session(
        self,
        *,
        system_prompt: str,
        prompt: str,
        tools: Sequence[AgentToolDefinition],
        max_output_tokens: int | None = None,
    ) -> AgentSession | None: ...
