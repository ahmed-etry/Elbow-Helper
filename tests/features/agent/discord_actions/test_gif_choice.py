"""Synthetic image and text choices preserve replies when no candidate fits."""

from dataclasses import replace
import asyncio
import io
import json
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch

from PIL import Image

from elbow_helper.features.agent.discord_actions.gif_choice import choose_gif
from elbow_helper.infrastructure.ai import (
    AgentStep, AgentUsage, TextGenerationError, DeepSeekTextClient, GenerationTier,
)
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
                                   f"https://media.giphy.com/{index}.png",
                                   f"Synthetic title {index}", f"Synthetic description {index}")
                      for index in range(8)]
        client = SimpleNamespace(search_candidates=AsyncMock(return_value=candidates),
                                 download_still=AsyncMock(return_value=png("red")))
        session = SimpleNamespace(advance=AsyncMock(return_value=AgentStep("4", (), AgentUsage())))
        model = SimpleNamespace(create_agent_session=MagicMock(return_value=session),
                                complete=AsyncMock(return_value="4"))
        context = replace(context, bot=SimpleNamespace(gif_client=client, agent_model=model))
        return context, client, model, session

    async def test_numbered_sheet_and_one_short_choice_call(self):
        context, client, model, session = self.context()
        logger = "elbow_helper.features.agent.discord_actions.gif_choice"
        with self.assertLogs(logger, "INFO") as log:
            result = await choose_gif(context, "synthetic happy")
        self.assertEqual(result, "https://giphy.com/gifs/synthetic-3")
        client.search_candidates.assert_awaited_once_with("synthetic happy", limit=8)
        self.assertEqual(client.download_still.await_count, 8)
        request = model.create_agent_session.call_args.kwargs
        self.assertIn("Synthetic celebration", request["prompt"])
        self.assertIn("synthetic happy", request["prompt"])
        self.assertFalse(request["image_failure_fallback"])
        image = Image.open(io.BytesIO(request["images"][0].data))
        self.assertEqual(image.size, (720, 600))
        for index in range(8):
            self.assertEqual(image.getpixel((index % 3 * 240 + 5, index // 3 * 200 + 25)),
                             (255, 0, 0))
        session.advance.assert_awaited_once()
        self.assertEqual(session.advance.await_args.kwargs["max_output_tokens"], 8)
        self.assertIn("candidate=4 mode=vision", log.output[0])
        self.assertNotIn("celebration", log.output[0])

    async def test_invalid_choice_and_no_fit_keep_text_on_either_path(self):
        for images in (True, False):
            for result in ("20", "-1", "invalid", "0", "1"):
                with self.subTest(images=images, result=result):
                    context, client, model, session = self.context()
                    model.supports_images = images
                    session.advance.return_value = AgentStep(
                        result, (), AgentUsage(), output_limit_reached=result == "1")
                    model.complete.return_value = None if result == "1" else result
                    logger = "elbow_helper.features.agent.discord_actions.gif_choice"
                    with self.assertLogs(logger, "INFO") as log:
                        self.assertIsNone(await choose_gif(context, "synthetic"))
                    if result == "0":
                        path = "vision" if images else "text"
                        self.assertIn(f"mode={path}_no_fit", log.output[0])
                    self.assertEqual(session.advance.await_count, int(images))
                    self.assertEqual(model.complete.await_count, int(not images or result != "0"))

    async def test_text_choice_uses_titles_descriptions_without_downloading_images(self):
        context, client, model, session = self.context()
        model.supports_images = False
        with self.assertLogs("elbow_helper.features.agent.discord_actions.gif_choice", "INFO") as log:
            result = await choose_gif(context, "synthetic happy")
        self.assertEqual(result, "https://giphy.com/gifs/synthetic-3")
        client.download_still.assert_not_awaited()
        model.create_agent_session.assert_not_called()
        request = model.complete.call_args.kwargs
        self.assertNotIn("images", request)
        self.assertEqual(request["tier"], GenerationTier.ROUTINE)
        self.assertEqual(request["max_output_tokens"], 8)
        self.assertIn("Synthetic celebration", request["prompt"])
        self.assertIn("synthetic happy", request["prompt"])
        metadata = json.loads(request["prompt"].split("\nCandidates: ", 1)[1])
        self.assertEqual(metadata[3], {"number": 4, "title": "Synthetic title 3",
                                       "description": "Synthetic description 3"})
        model.complete.assert_awaited_once()
        session.advance.assert_not_awaited()
        # The existing harness parses this log into gif_decision.vision_mode.
        self.assertIn("candidate=4 mode=text_pick", log.output[0])
        self.assertNotIn("celebration", log.output[0])

    async def test_unavailable_vision_falls_back_to_text_once(self):
        for failure in ("still", "call", "no_model"):
            context, client, model, session = self.context()
            if failure == "still":
                client.download_still.return_value = None
            elif failure == "call":
                session.advance.side_effect = TextGenerationError("Synthetic image rejection")
            else:
                model.create_agent_session.return_value = None
            self.assertEqual(await choose_gif(context, "synthetic"),
                             "https://giphy.com/gifs/synthetic-3")
            model.complete.assert_awaited_once()
            self.assertEqual(session.advance.await_count, int(failure == "call"))

    async def test_unusable_vision_reply_falls_back_and_records_text_pick(self):
        replies = (
            # The live harness received this empty, truncated vision response.
            AgentStep("", (), AgentUsage(), output_limit_reached=True),
            AgentStep("4", (), AgentUsage(), output_limit_reached=True),
            AgentStep("", (), AgentUsage()),
            AgentStep(None, (), AgentUsage()),
            AgentStep("invalid", (), AgentUsage()),
            AgentStep("20", (), AgentUsage()),
            AgentStep("4", (object(),), AgentUsage()),
        )
        for reply in replies:
            with self.subTest(reply=reply):
                context, client, model, session = self.context()
                # Like the harness wrapper, this model omits supports_images.
                session.advance.return_value = reply
                with self.assertLogs("elbow_helper.features.agent.discord_actions.gif_choice", "INFO") as log:
                    result = await choose_gif(context, "laughing cat")
                self.assertEqual(result, "https://giphy.com/gifs/synthetic-3")
                session.advance.assert_awaited_once()
                model.complete.assert_awaited_once()
                client.search_candidates.assert_awaited_once_with("laughing cat", limit=8)
                self.assertIn("candidate=4 mode=text_pick", log.output[0])

    async def test_vision_timeout_still_allows_bounded_text_fallback(self):
        context, _, model, session = self.context()

        async def stalled_vision(**kwargs):
            await asyncio.Event().wait()

        session.advance.side_effect = stalled_vision
        with patch("elbow_helper.features.agent.discord_actions.gif_choice.time_left", return_value=0.05):
            self.assertEqual(await choose_gif(context, "laughing cat"),
                             "https://giphy.com/gifs/synthetic-3")
        model.complete.assert_awaited_once()

    async def test_expired_deadline_prevents_text_fallback(self):
        context, _, model, session = self.context()
        session.advance.side_effect = TextGenerationError("Synthetic image rejection")
        with patch("elbow_helper.features.agent.discord_actions.gif_choice.time_left",
                   side_effect=[10, 10, 10, 10, 0]):
            self.assertIsNone(await choose_gif(context, "laughing cat"))
        model.complete.assert_not_awaited()

    async def test_empty_search_retries_once_with_core_words_and_preserves_intent(self):
        context, client, model, _ = self.context()
        model.supports_images = False
        client.search_candidates.side_effect = [[], client.search_candidates.return_value]
        self.assertEqual(await choose_gif(context, "confident robot shrug"),
                         "https://giphy.com/gifs/synthetic-3")
        self.assertEqual(client.search_candidates.await_args_list, [
            call("confident robot shrug", limit=8), call("robot shrug", limit=8),
        ])
        self.assertIn("Query: confident robot shrug", model.complete.await_args.kwargs["prompt"])
        model.complete.assert_awaited_once()

    async def test_two_empty_searches_stop_without_selection(self):
        context, client, model, _ = self.context()
        client.search_candidates.return_value = []
        self.assertIsNone(await choose_gif(context, "confident robot shrug"))
        self.assertEqual(client.search_candidates.await_args_list, [
            call("confident robot shrug", limit=8), call("robot shrug", limit=8),
        ])
        model.create_agent_session.assert_not_called()
        model.complete.assert_not_awaited()
        client.download_still.assert_not_awaited()

    async def test_short_empty_queries_do_not_repeat_identical_search(self):
        for query, expected in (("laughing cat", ["laughing cat", "cat"]), ("shrug", ["shrug"])):
            with self.subTest(query=query):
                context, client, model, _ = self.context()
                client.search_candidates.return_value = []
                self.assertIsNone(await choose_gif(context, query))
                self.assertEqual(client.search_candidates.await_args_list,
                                 [call(item, limit=8) for item in expected])
                model.complete.assert_not_awaited()

    async def test_search_transport_failure_does_not_retry(self):
        context, client, model, _ = self.context()
        client.search_candidates.side_effect = TimeoutError
        self.assertIsNone(await choose_gif(context, "confident robot shrug"))
        client.search_candidates.assert_awaited_once()
        model.complete.assert_not_awaited()

    async def test_expired_deadline_prevents_broader_search(self):
        context, client, model, _ = self.context()
        client.search_candidates.return_value = []
        with patch("elbow_helper.features.agent.discord_actions.gif_choice.time_left",
                   side_effect=[10, 10, 0]):
            self.assertIsNone(await choose_gif(context, "confident robot shrug"))
        client.search_candidates.assert_awaited_once_with("confident robot shrug", limit=8)
        model.complete.assert_not_awaited()

    async def test_missing_metadata_and_unavailable_text_model_keep_text(self):
        for failure in ("metadata", "call", "no_model", "empty_pick"):
            context, client, model, session = self.context()
            model.supports_images = False
            if failure in ("metadata", "empty_pick"):
                client.search_candidates.return_value = [
                    GifCandidate("https://giphy.com/gifs/empty", None),
                    GifCandidate("https://giphy.com/gifs/other", None,
                                 title="" if failure == "metadata" else "Synthetic other"),
                ]
                model.complete.return_value = "1"
            elif failure == "call":
                model.complete.side_effect = TextGenerationError("Synthetic failure")
            else:
                model.complete.return_value = None
            self.assertIsNone(await choose_gif(context, "synthetic"))
            client.download_still.assert_not_awaited()
            self.assertLessEqual(model.complete.await_count, 1)

    async def test_deepseek_declares_text_only_and_skips_image_work(self):
        context, client, _, _ = self.context()
        model = DeepSeekTextClient(None)
        self.assertFalse(model.supports_images)
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="4", reasoning_content=None), finish_reason="stop",
        )])
        create = AsyncMock(return_value=response)
        model._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        context = replace(context, bot=SimpleNamespace(gif_client=client, agent_model=model))
        self.assertEqual(await choose_gif(context, "synthetic"),
                         "https://giphy.com/gifs/synthetic-3")
        client.download_still.assert_not_awaited()
        create.assert_awaited_once()
        request = create.await_args.kwargs
        self.assertEqual(request["max_tokens"], 8)
        self.assertEqual(request["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertNotIn("tools", request)
        self.assertIsInstance(request["messages"][1]["content"], str)

    async def test_expired_deadline_starts_no_search(self):
        context, client, model, _ = self.context()
        context = replace(context, deadline_monotonic=time.monotonic() - 1)
        self.assertIsNone(await choose_gif(context, "synthetic"))
        model.create_agent_session.assert_not_called()

    async def test_candidate_search_keeps_rating_language_and_requests_eight(self):
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
            "api_key": "synthetic", "q": "synthetic", "limit": 8,
            "rating": "pg-13", "lang": "en",
        })

    async def test_search_retains_eight_distinct_safe_candidates_with_provider_text(self):
        from elbow_helper.infrastructure.gifs import GiphyClient
        client = GiphyClient("synthetic")
        rows = [{"url": f"https://giphy.com/gifs/synthetic-{index}",
                 "title": f"Title {index}", "alt_text": f"Description {index}"}
                for index in range(10)]
        response = SimpleNamespace(status=200, json=AsyncMock(return_value={"data": [
            None, {"url": "http://giphy.com/gifs/unsafe"},
            {"url": "https://untrusted.invalid/gif"}, rows[0], *rows,
        ]}))
        request = MagicMock()
        request.__aenter__ = AsyncMock(return_value=response)
        request.__aexit__ = AsyncMock()
        client._session = SimpleNamespace(closed=False, get=MagicMock(return_value=request))
        candidates = await client.search_candidates("synthetic")
        self.assertEqual(len(candidates), 8)
        self.assertEqual([item.title for item in candidates], [f"Title {i}" for i in range(8)])
        self.assertEqual(candidates[7].description, "Description 7")
        self.assertIsNone(candidates[7].still_url)

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
