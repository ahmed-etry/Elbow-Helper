"""Images stay in provider content, outside conversation records."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord
import httpx
from openai import BadRequestError

from elbow_helper.features.agent.files.images import request_images
from elbow_helper.infrastructure.ai.agent import AgentImage
from elbow_helper.infrastructure.ai.client import _DeepSeekAgentSession


class ImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_discord_and_io_failures_skip_images_without_hiding_other_errors(self):
        errors = (
            discord.HTTPException(SimpleNamespace(status=503, reason="Unavailable"), "Synthetic"),
            OSError("Synthetic"),
            asyncio.TimeoutError(),
        )
        attachment = SimpleNamespace(
            content_type="image/png", filename="synthetic.png", size=3, read=AsyncMock(),
        )
        proxy = SimpleNamespace(proxy_url="https://example.invalid/synthetic.png")
        source = SimpleNamespace(
            id=1, attachments=[attachment],
            embeds=[SimpleNamespace(type="image", image=proxy, thumbnail=proxy)],
        )
        download = AsyncMock()
        context = SimpleNamespace(
            source_message=source, attachment_sources=(source,),
            bot=SimpleNamespace(http=SimpleNamespace(get_from_cdn=download)),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                attachment.read.side_effect = error
                download.side_effect = error
                self.assertEqual(await request_images(context), ())
        attachment.read.side_effect = ValueError("Synthetic unexpected failure")
        with self.assertRaises(ValueError):
            await request_images(context)

    async def test_request_sources_order_and_cap(self):
        def message(identifier):
            return SimpleNamespace(id=identifier, embeds=[], attachments=[
                SimpleNamespace(
                    content_type="image/png", filename="synthetic.png", size=3,
                    read=AsyncMock(return_value=b"png"),
                )
                for _ in range(3)
            ])
        source, reply = message(1), message(2)
        context = SimpleNamespace(source_message=source, attachment_sources=(source, reply))
        images = await request_images(context)
        self.assertEqual(len(images), 4)
        self.assertIn("asker's message", images[0].label)
        self.assertIn("replied-to", images[-1].label)

    async def test_provider_recreates_text_session_once_on_first_400(self):
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="Synthetic answer", tool_calls=[]),
            finish_reason="stop",
        )], usage=None)
        error = BadRequestError(
            "Synthetic image rejection",
            response=httpx.Response(400, request=httpx.Request("POST", "https://example.invalid")),
            body=None,
        )
        create = AsyncMock(side_effect=[error, response, response])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        session = _DeepSeekAgentSession(
            client=client, system_prompt="Synthetic", prompt="Request", tools=(),
            max_output_tokens=100, images=[AgentImage("image/png", b"png", "Image 1")],
        )
        self.assertEqual((await session.advance()).content, "Synthetic answer")
        parts = create.await_args_list[0].kwargs["messages"][1]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "Request"})
        self.assertTrue(parts[2]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertIn(
            "could not be viewed", create.await_args_list[1].kwargs["messages"][1]["content"],
        )
        await session.advance()
        self.assertIsInstance(create.await_args_list[-1].kwargs["messages"][1]["content"], str)
