"""Synthetic candidate stills select once, fall back predictably and respect the deadline."""

from dataclasses import replace
import io
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

from PIL import Image

from elbow_helper.features.agent.discord_actions.gif_choice import choose_gif
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage, TextGenerationError
from elbow_helper.infrastructure.gifs.giphy import GifCandidate
from features.agent.engine.test_agent_service import _context


def png(colour):
    output = io.BytesIO()
    Image.new("RGB", (20, 20), colour).save(output, format="PNG")
    return output.getvalue()


class GifChoiceTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        context = _context()
        context.source_message.id = 300
        context.state.request_text = "Synthetic celebration"
        candidates = [GifCandidate(f"https://giphy.com/gifs/synthetic-{index}",
                                   f"https://media.giphy.com/{index}.png") for index in range(6)]
        client = SimpleNamespace(search_candidates=AsyncMock(return_value=candidates),
                                 download_still=AsyncMock(return_value=png("red")))
        session = SimpleNamespace(advance=AsyncMock(return_value=AgentStep("4", (), AgentUsage())))
        model = SimpleNamespace(create_agent_session=MagicMock(return_value=session))
        context = replace(context, bot=SimpleNamespace(gif_client=client, agent_model=model))
        return context, client, model, session

    async def test_numbered_sheet_and_one_short_choice_call(self):
        context, client, model, session = self.context()
        logger = "elbow_helper.features.agent.discord_actions.gif_choice"
        with self.assertLogs(logger, "INFO") as log:
            result = await choose_gif(context, "synthetic happy")
        self.assertEqual(result, "https://giphy.com/gifs/synthetic-3")
        self.assertEqual(client.download_still.await_count, 6)
        request = model.create_agent_session.call_args.kwargs
        self.assertIn("Synthetic celebration", request["prompt"])
        self.assertIn("synthetic happy", request["prompt"])
        self.assertFalse(request["image_failure_fallback"])
        image = Image.open(io.BytesIO(request["images"][0].data))
        self.assertEqual(image.size, (720, 400))
        for index in range(6):
            self.assertEqual(image.getpixel((index % 3 * 240 + 5, index // 3 * 200 + 25)),
                             (255, 0, 0))
        session.advance.assert_awaited_once()
        self.assertEqual(session.advance.await_args.kwargs["max_output_tokens"], 8)
        self.assertIn("candidate=4 mode=vision", log.output[0])
        self.assertNotIn("celebration", log.output[0])

    async def test_still_call_and_invalid_choice_failures_use_the_top_result(self):
        for failure in ("still", "call", "choice"):
            context, client, model, session = self.context()
            if failure == "still":
                client.download_still.return_value = None
            elif failure == "call":
                session.advance.side_effect = TextGenerationError("Synthetic failure")
            else:
                session.advance.return_value = AgentStep("20", (), AgentUsage())
            self.assertEqual(await choose_gif(context, "synthetic"),
                             "https://giphy.com/gifs/synthetic-0")
            self.assertLessEqual(session.advance.await_count, 1)
            if failure == "still":
                model.create_agent_session.assert_not_called()

    async def test_expired_deadline_starts_no_search(self):
        context, client, model, _ = self.context()
        context = replace(context, deadline_monotonic=time.monotonic() - 1)
        self.assertIsNone(await choose_gif(context, "synthetic"))
        model.create_agent_session.assert_not_called()

    async def test_candidate_search_keeps_rating_language_and_requests_six(self):
        from elbow_helper.infrastructure.gifs import GiphyClient
        client = GiphyClient("synthetic")
        response = SimpleNamespace(status=200, json=AsyncMock(return_value={"data": [
            {"url": "https://giphy.com/gifs/synthetic", "images": {"original_still": {
                "url": "https://media.giphy.com/synthetic.png",
            }}},
        ]}))
        request = MagicMock()
        request.__aenter__ = AsyncMock(return_value=response)
        request.__aexit__ = AsyncMock()
        client._session = SimpleNamespace(closed=False, get=MagicMock(return_value=request))
        self.assertEqual(len(await client.search_candidates("synthetic")), 1)
        self.assertEqual(client._session.get.call_args.kwargs["params"], {
            "api_key": "synthetic", "q": "synthetic", "limit": 6,
            "rating": "pg-13", "lang": "en",
        })

    async def test_image_rejection_does_not_make_a_second_model_call(self):
        import httpx
        from openai import BadRequestError
        from elbow_helper.infrastructure.ai.client import _DeepSeekAgentSession
        from elbow_helper.infrastructure.ai.agent import AgentImage
        failure = BadRequestError("Synthetic", response=httpx.Response(
            400, request=httpx.Request("POST", "https://synthetic.invalid")), body=None)
        create = AsyncMock(side_effect=failure)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        session = _DeepSeekAgentSession(
            client=client, system_prompt="Synthetic", prompt="Synthetic", tools=(),
            max_output_tokens=8, images=(AgentImage("image/png", png("red"), "Synthetic"),),
            image_failure_fallback=False,
        )
        with self.assertRaises(TextGenerationError):
            await session.advance(allow_tools=False)
        create.assert_awaited_once()
