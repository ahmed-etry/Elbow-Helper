"""Shared asynchronous access to configured text generation."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any
from typing import Protocol

from openai import AsyncOpenAI
from openai import OpenAIError

from .agent import AgentModel
from .agent import AgentSession
from .agent import AgentStep
from .agent import AgentToolCall
from .agent import AgentToolDefinition
from .agent import AgentToolResult
from .agent import AgentUsage


DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-flash"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 2
PROVIDER_ERROR_MESSAGE_LIMIT = 500


class TextGenerationError(RuntimeError):
    """Raised when a configured text-generation request fails."""


class GenerationTier(StrEnum):
    """Provider-neutral amount of generation work requested by a feature."""

    ROUTINE = "routine"
    COMPLEX = "complex"


class TextGenerator(Protocol):
    """Contract consumed by features that need generated text."""

    @property
    def configured(self) -> bool: ...

    async def complete(
        self,
        *,
        tier: GenerationTier,
        prompt: str,
        temperature: float,
        system_prompt: str | None = None,
        max_output_tokens: int | None = None,
    ) -> str | None: ...


class AIClient(TextGenerator, AgentModel, Protocol):
    """Complete shared AI capability owned by the application lifecycle."""


class DeepSeekTextClient:
    """Generate text through DeepSeek's OpenAI-compatible API."""

    def __init__(self, api_key: str | None):
        self._client = (
            AsyncOpenAI(
                api_key=api_key,
                base_url=DEEPSEEK_BASE_URL,
                timeout=DEFAULT_TIMEOUT_SECONDS,
                max_retries=DEFAULT_MAX_RETRIES,
            )
            if api_key
            else None
        )

    @property
    def configured(self) -> bool:
        return self._client is not None

    async def complete(
        self,
        *,
        tier: GenerationTier,
        prompt: str,
        temperature: float,
        system_prompt: str | None = None,
        max_output_tokens: int | None = None,
    ) -> str | None:
        if self._client is None:
            return None
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        options: dict[str, Any] = {
            "model": DEEPSEEK_MODEL,
            "messages": messages,
        }
        if tier is GenerationTier.COMPLEX:
            options["reasoning_effort"] = "high"
            options["extra_body"] = {"thinking": {"type": "enabled"}}
        else:
            options["temperature"] = temperature
            options["extra_body"] = {"thinking": {"type": "disabled"}}
        if max_output_tokens is not None:
            options["max_tokens"] = max_output_tokens
        try:
            response = await self._client.chat.completions.create(**options)
            choice = response.choices[0]
            message = getattr(choice, "message", None)
            if isinstance(message, dict):
                content = message.get("content")
                reasoning_content = message.get("reasoning_content")
            else:
                content = getattr(message, "content", None)
                reasoning_content = getattr(message, "reasoning_content", None)
            if not content:
                content = getattr(choice, "text", None)
        except OpenAIError as error:
            raise TextGenerationError(
                self._format_provider_error(error)
            ) from error
        except (
            AttributeError,
            IndexError,
            KeyError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as error:
            raise TextGenerationError(
                f"DeepSeek returned an invalid response ({type(error).__name__})"
            ) from error
        cleaned = str(content or "").strip()
        if cleaned:
            return cleaned

        details = []
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason is not None:
            details.append(f"finish_reason={finish_reason}")
        if reasoning_content:
            details.append(f"reasoning_chars={len(str(reasoning_content))}")
        completion_tokens = getattr(
            getattr(response, "usage", None),
            "completion_tokens",
            None,
        )
        if completion_tokens is not None:
            details.append(f"completion_tokens={completion_tokens}")
        diagnostic = " ".join(details) or "no response details"
        raise TextGenerationError(
            f"DeepSeek returned no final content ({diagnostic})"
        )

    def create_agent_session(
        self,
        *,
        system_prompt: str,
        prompt: str,
        tools: Sequence[AgentToolDefinition],
        max_output_tokens: int | None = None,
    ) -> AgentSession | None:
        """Create a tool-capable session without exposing provider messages."""

        if self._client is None:
            return None
        return _DeepSeekAgentSession(
            client=self._client,
            system_prompt=system_prompt,
            prompt=prompt,
            tools=tools,
            max_output_tokens=max_output_tokens,
        )

    async def close(self) -> None:
        """Close the provider transport when it was configured."""

        if self._client is not None:
            await self._client.close()

    async def __aenter__(self) -> DeepSeekTextClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    @staticmethod
    def _format_provider_error(error: OpenAIError) -> str:
        """Preserve useful private diagnostics without exposing credentials."""

        parts = [f"type={type(error).__name__}"]
        status_code = getattr(error, "status_code", None)
        if status_code is not None:
            parts.append(f"status={status_code}")
        code = getattr(error, "code", None)
        if code is not None:
            parts.append(f"code={code}")
        message = " ".join(str(error).split())
        if message:
            if len(message) > PROVIDER_ERROR_MESSAGE_LIMIT:
                message = f"{message[:PROVIDER_ERROR_MESSAGE_LIMIT - 3]}..."
            parts.append(f"message={message}")
        return f"DeepSeek text generation failed: {' '.join(parts)}"


class _DeepSeekAgentSession:
    """Keep DeepSeek-specific reasoning and tool state inside infrastructure."""

    def __init__(
        self,
        *,
        client: AsyncOpenAI,
        system_prompt: str,
        prompt: str,
        tools: Sequence[AgentToolDefinition],
        max_output_tokens: int | None,
    ):
        self._client = client
        self._messages: list[Any] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
        self._tools = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": dict(tool.parameters),
                },
            }
            for tool in tools
        ]
        self._max_output_tokens = max_output_tokens

    async def advance(
        self,
        tool_results: Sequence[AgentToolResult] = (),
        *,
        allow_tools: bool = True,
    ) -> AgentStep:
        for result in tool_results:
            self._messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.call_id,
                    "content": result.content,
                }
            )

        options: dict[str, Any] = {
            "model": DEEPSEEK_MODEL,
            "messages": self._messages,
            "reasoning_effort": "high",
            "extra_body": {"thinking": {"type": "enabled"}},
        }
        if self._tools:
            options["tools"] = self._tools
            options["tool_choice"] = "auto" if allow_tools else "none"
        if self._max_output_tokens is not None:
            options["max_tokens"] = self._max_output_tokens

        try:
            response = await self._client.chat.completions.create(**options)
            choice = response.choices[0]
            message = choice.message
            content = _message_value(message, "content")
            raw_tool_calls = _message_value(message, "tool_calls") or ()
            tool_calls = tuple(
                _parse_agent_tool_call(raw_tool_call)
                for raw_tool_call in raw_tool_calls
            )
        except OpenAIError as error:
            raise TextGenerationError(
                DeepSeekTextClient._format_provider_error(error)
            ) from error
        except (
            AttributeError,
            IndexError,
            KeyError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as error:
            raise TextGenerationError(
                f"DeepSeek returned an invalid agent response ({type(error).__name__})"
            ) from error

        # DeepSeek requires the complete assistant message, including its
        # reasoning_content, on every later tool-calling request.
        self._messages.append(message)

        cleaned = str(content or "").strip()
        if not cleaned and not tool_calls:
            raise TextGenerationError(
                "DeepSeek returned no agent content or tool calls"
            )

        usage = getattr(response, "usage", None)
        return AgentStep(
            content=cleaned,
            tool_calls=tool_calls,
            usage=AgentUsage(
                prompt_tokens=_usage_value(usage, "prompt_tokens"),
                completion_tokens=_usage_value(usage, "completion_tokens"),
                prompt_cache_hit_tokens=_usage_value(
                    usage,
                    "prompt_cache_hit_tokens",
                ),
                prompt_cache_miss_tokens=_usage_value(
                    usage,
                    "prompt_cache_miss_tokens",
                ),
            ),
        )


def _message_value(message: object, key: str) -> Any:
    if isinstance(message, dict):
        return message.get(key)
    return getattr(message, key, None)


def _parse_agent_tool_call(raw: object) -> AgentToolCall:
    call_id = _message_value(raw, "id")
    function = _message_value(raw, "function")
    name = _message_value(function, "name")
    arguments = _message_value(function, "arguments")
    if not call_id or not name:
        raise ValueError("Tool call is missing an ID or function name")
    return AgentToolCall(
        call_id=str(call_id),
        name=str(name),
        arguments=str(arguments or "{}"),
    )


def _usage_value(usage: object, key: str) -> int:
    value = _message_value(usage, key)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
