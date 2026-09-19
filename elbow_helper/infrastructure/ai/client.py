"""Shared asynchronous access to configured text generation."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
import json
import re
import time
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
DEEPSEEK_CONTEXT_WINDOW_TOKENS = 1_000_000
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 2
PROVIDER_ERROR_MESSAGE_LIMIT = 500
_FINAL_ANSWER_INSTRUCTION = (
    "Research for this request has ended. Do not request or simulate any more "
    "tool calls. Answer the user's request now using the evidence already "
    "available in this conversation. Give useful supported findings even if "
    "the investigation is incomplete, and state any material gaps without "
    "inventing facts or claiming that unperformed work was completed. "
    "Return only the user-facing answer, without tool-call markup."
)
_DSML_TAG = r"[|\uFF5C]{2}DSML[|\uFF5C]{2}"
_DSML_MARKER = re.compile(_DSML_TAG, re.IGNORECASE)
_DSML_INVOKE = re.compile(
    rf'<\s*{_DSML_TAG}\s+invoke\s+name="([^"]+)"\s*>'
    rf'(.*?)</\s*{_DSML_TAG}\s+invoke\s*>',
    re.IGNORECASE | re.DOTALL,
)
_DSML_PARAMETER = re.compile(
    rf'<\s*{_DSML_TAG}\s+parameter\s+name="([^"]+)"'
    rf'\s+string="(true|false)"\s*>(.*?)'
    rf'</\s*{_DSML_TAG}\s+parameter\s*>',
    re.IGNORECASE | re.DOTALL,
)


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

    @property
    def context_window_tokens(self) -> int:
        return DEEPSEEK_CONTEXT_WINDOW_TOKENS

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
        self.replace_tools(tools)
        self._max_output_tokens = max_output_tokens
        self._text_tool_call_sequence = 0

    def replace_tools(self, tools: Sequence[AgentToolDefinition]) -> None:
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

        request_messages = list(self._messages)
        if not allow_tools:
            # Keep this trusted instruction in the initial system message;
            # it describes the runtime phase, not a new user request.
            request_messages[0] = {
                **request_messages[0],
                "content": request_messages[0]["content"]
                + "\n\n" + _FINAL_ANSWER_INSTRUCTION,
            }
        options: dict[str, Any] = {
            "model": DEEPSEEK_MODEL,
            "messages": request_messages,
            "reasoning_effort": "high",
            "extra_body": {"thinking": {"type": "enabled"}},
        }
        # Keep the tools parameter so DeepSeek retains earlier reasoning in
        # context. Disable calls explicitly when the research phase has ended.
        if self._tools:
            options["tools"] = self._tools
        if not allow_tools:
            options["tool_choice"] = "none"
        if self._max_output_tokens is not None:
            options["max_tokens"] = self._max_output_tokens

        started_at = time.monotonic()
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
            recovered_tool_calls = (
                None
                if tool_calls
                else _parse_dsml_tool_calls(
                    str(content or ""),
                    allowed_names={
                        tool["function"]["name"] for tool in self._tools
                    },
                    sequence=self._text_tool_call_sequence,
                )
            )
            if tool_calls and _DSML_MARKER.search(str(content or "")):
                # Some provider responses duplicate a structured call in the
                # text field. Keep the structured call and never expose its
                # control representation as answer text.
                content = ""
            if recovered_tool_calls is not None:
                # Preserve valid calls even when the provider ignores none.
                # The orchestrator must decline them and can request a final
                # answer with those refusals; the adapter never executes tools.
                self._text_tool_call_sequence += 1
                tool_calls = recovered_tool_calls
                content = ""
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call.call_id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": call.arguments,
                            },
                        }
                        for call in tool_calls
                    ],
                }
                reasoning_content = _message_value(
                    choice.message, "reasoning_content",
                )
                if reasoning_content is not None:
                    message["reasoning_content"] = reasoning_content
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
            detail = _diagnostic_text(str(error), maximum=180)
            description = type(error).__name__
            if detail:
                description += f": {detail}"
            raise TextGenerationError(
                f"DeepSeek returned an invalid agent response ({description})"
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
            provider_request_id=_diagnostic_text(
                getattr(response, "id", None), maximum=200,
            ),
            model_identity=_diagnostic_text(
                getattr(response, "model", None), maximum=100,
            ) or DEEPSEEK_MODEL,
            provider_duration_ms=max(
                0, int((time.monotonic() - started_at) * 1_000),
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


def _parse_dsml_tool_calls(
    content: str, *, allowed_names: set[str], sequence: int,
) -> tuple[AgentToolCall, ...] | None:
    """Recover DeepSeek control calls emitted as text; reject malformed markup."""

    if _DSML_MARKER.search(content) is None:
        return None
    normalized = content.replace("\\</", "</").strip()
    invocations = list(_DSML_INVOKE.finditer(normalized))
    if not invocations:
        raise ValueError("Malformed provider control markup")
    remainder = _DSML_INVOKE.sub("", normalized)
    remainder = re.sub(
        rf'</?\s*{_DSML_TAG}\s+calls\s*>', "", remainder,
        flags=re.IGNORECASE,
    ).strip()
    # A model may introduce a call with ordinary prose. It is not a final
    # answer, so discard it while still rejecting any unparsed control tags.
    if _DSML_MARKER.search(remainder):
        raise ValueError("Malformed provider control markup")

    calls = []
    for index, invocation in enumerate(invocations):
        name, body = invocation.groups()
        if name not in allowed_names:
            raise ValueError("Provider requested an unavailable tool")
        parameters = list(_DSML_PARAMETER.finditer(body))
        if _DSML_PARAMETER.sub("", body).strip():
            raise ValueError("Malformed provider tool parameters")
        arguments: dict[str, Any] = {}
        for parameter in parameters:
            parameter_name, is_string, raw_value = parameter.groups()
            if parameter_name in arguments:
                raise ValueError("Provider repeated a tool parameter")
            value = raw_value.strip()
            if is_string.casefold() == "true":
                arguments[parameter_name] = value
            else:
                arguments[parameter_name] = json.loads(value)
        calls.append(AgentToolCall(
            call_id=f"dsml-{sequence}-{index}",
            name=name,
            arguments=json.dumps(arguments, ensure_ascii=False),
        ))
    return tuple(calls)


def _usage_value(usage: object, key: str) -> int | None:
    value = _message_value(usage, key)
    # Do not turn missing/malformed counters into apparently free usage.
    return value if type(value) is int and value >= 0 else None


def _diagnostic_text(value: object, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:maximum] if cleaned else None
