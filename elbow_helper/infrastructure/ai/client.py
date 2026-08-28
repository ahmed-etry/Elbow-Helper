"""Shared asynchronous access to OpenAI text generation."""

from __future__ import annotations

import asyncio
from typing import Any
from typing import Protocol

from openai import OpenAI
from openai import OpenAIError


class TextGenerationError(RuntimeError):
    """Raised when a configured text-generation request fails."""


class TextGenerator(Protocol):
    """Contract consumed by features that need generated text."""

    @property
    def configured(self) -> bool: ...

    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        temperature: float,
        max_tokens: int | None = None,
        max_completion_tokens: int | None = None,
    ) -> str | None: ...


class OpenAITextClient:
    """Run synchronous OpenAI SDK calls without blocking Discord's event loop."""

    def __init__(self, api_key: str | None):
        self._client = OpenAI(api_key=api_key) if api_key else None

    @property
    def configured(self) -> bool:
        return self._client is not None

    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        temperature: float,
        max_tokens: int | None = None,
        max_completion_tokens: int | None = None,
    ) -> str | None:
        if self._client is None:
            return None
        options: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }
        if max_tokens is not None:
            options["max_tokens"] = max_tokens
        if max_completion_tokens is not None:
            options["max_completion_tokens"] = max_completion_tokens
        try:
            response = await asyncio.to_thread(
                self._client.chat.completions.create,
                **options,
            )
            choice = response.choices[0]
            message = getattr(choice, "message", None)
            if isinstance(message, dict):
                content = message.get("content")
            else:
                content = getattr(message, "content", None)
            if not content:
                content = getattr(choice, "text", None)
        except (
            OpenAIError,
            AttributeError,
            IndexError,
            KeyError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as error:
            raise TextGenerationError(
                "OpenAI text generation failed"
            ) from error
        cleaned = str(content or "").strip()
        return cleaned or None
