from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

from openai import OpenAIError

from elbow_helper.infrastructure.ai import DeepSeekTextClient
from elbow_helper.infrastructure.ai import GenerationTier
from elbow_helper.infrastructure.ai import TextGenerationError
from elbow_helper.infrastructure.ai.client import DEEPSEEK_BASE_URL
from elbow_helper.infrastructure.ai.client import DEEPSEEK_MODEL


class DeepSeekTextClientTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _response(
        content: str,
        *,
        reasoning_content: str | None = None,
        finish_reason: str = "stop",
        completion_tokens: int | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=content,
                        reasoning_content=reasoning_content,
                    ),
                    finish_reason=finish_reason,
                )
            ],
            usage=SimpleNamespace(completion_tokens=completion_tokens),
        )

    async def test_unconfigured_client_skips_generation(self) -> None:
        with patch("elbow_helper.infrastructure.ai.client.AsyncOpenAI") as constructor:
            client = DeepSeekTextClient(None)

            result = await client.complete(
                tier=GenerationTier.ROUTINE,
                prompt="hello",
                temperature=0.2,
            )

        self.assertIsNone(result)
        constructor.assert_not_called()

    async def test_routine_request_uses_deepseek_non_thinking_chat_completion(self) -> None:
        transport = MagicMock()
        transport.chat.completions.create = AsyncMock(
            return_value=self._response("  generated text  ")
        )
        transport.close = AsyncMock()

        with patch(
            "elbow_helper.infrastructure.ai.client.AsyncOpenAI",
            return_value=transport,
        ) as constructor:
            async with DeepSeekTextClient("deepseek-token") as client:
                result = await client.complete(
                    tier=GenerationTier.ROUTINE,
                    prompt="hello",
                    temperature=0.3,
                    max_output_tokens=120,
                )

        self.assertEqual(result, "generated text")
        constructor.assert_called_once_with(
            api_key="deepseek-token",
            base_url=DEEPSEEK_BASE_URL,
            timeout=60.0,
            max_retries=2,
        )
        transport.chat.completions.create.assert_awaited_once_with(
            model=DEEPSEEK_MODEL,
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.3,
            extra_body={"thinking": {"type": "disabled"}},
            max_tokens=120,
        )
        transport.close.assert_awaited_once_with()

    async def test_complex_tier_uses_the_current_deepseek_model(self) -> None:
        transport = MagicMock()
        transport.chat.completions.create = AsyncMock(
            return_value=self._response("answer")
        )

        with patch(
            "elbow_helper.infrastructure.ai.client.AsyncOpenAI",
            return_value=transport,
        ):
            client = DeepSeekTextClient("deepseek-token")
            await client.complete(
                tier=GenerationTier.COMPLEX,
                prompt="hard question",
                system_prompt="trusted instructions",
                temperature=0.1,
            )

        request = transport.chat.completions.create.await_args.kwargs
        self.assertEqual(request["model"], DEEPSEEK_MODEL)
        self.assertEqual(
            request["messages"],
            [
                {"role": "system", "content": "trusted instructions"},
                {"role": "user", "content": "hard question"},
            ],
        )
        self.assertEqual(request["reasoning_effort"], "high")
        self.assertEqual(
            request["extra_body"],
            {"thinking": {"type": "enabled"}},
        )
        self.assertNotIn("temperature", request)
        self.assertNotIn("max_tokens", request)

    async def test_provider_error_preserves_useful_diagnostics(self) -> None:
        transport = MagicMock()
        transport.chat.completions.create = AsyncMock(
            side_effect=OpenAIError("insufficient balance")
        )

        with patch(
            "elbow_helper.infrastructure.ai.client.AsyncOpenAI",
            return_value=transport,
        ):
            client = DeepSeekTextClient("deepseek-token")
            with self.assertRaisesRegex(
                TextGenerationError,
                r"DeepSeek text generation failed: type=OpenAIError.*insufficient balance",
            ):
                await client.complete(
                    tier=GenerationTier.ROUTINE,
                    prompt="hello",
                    temperature=0.2,
                )

    async def test_empty_final_response_preserves_reasoning_diagnostics(
        self,
    ) -> None:
        transport = MagicMock()
        transport.chat.completions.create = AsyncMock(
            return_value=self._response(
                "",
                reasoning_content="internal reasoning",
                finish_reason="length",
                completion_tokens=3_000,
            )
        )

        with patch(
            "elbow_helper.infrastructure.ai.client.AsyncOpenAI",
            return_value=transport,
        ):
            client = DeepSeekTextClient("deepseek-token")
            with self.assertRaisesRegex(
                TextGenerationError,
                r"DeepSeek returned no final content.*finish_reason=length.*reasoning_chars=18.*completion_tokens=3000",
            ):
                await client.complete(
                    tier=GenerationTier.COMPLEX,
                    prompt="hard question",
                    temperature=0.2,
                )

    def test_provider_error_message_is_bounded(self) -> None:
        rendered = DeepSeekTextClient._format_provider_error(
            OpenAIError("x" * 1_000)
        )

        self.assertLess(len(rendered), 600)
        self.assertTrue(rendered.endswith("..."))
