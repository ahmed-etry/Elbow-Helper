"""Shared asynchronous access to configured text generation."""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from typing import Protocol

from openai import AsyncOpenAI
from openai import OpenAIError


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
            else:
                content = getattr(message, "content", None)
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
        return cleaned or None

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
