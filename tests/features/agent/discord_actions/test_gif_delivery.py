"""Synthetic GIF replies retain one link and use separate tracked Discord messages."""

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.features.agent.delivery import AgentDeliveryMixin, _without_reply_gif
from elbow_helper.features.agent.discord_actions.gifs import find_gif
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.models import AgentDelivery
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage
from elbow_helper.infrastructure.gifs.giphy import GifCandidate
from features.agent.engine.test_agent_service import _AgentModel, _AgentSession, _context
from tests.features.agent_plan_helpers import plan_call


def candidate_client(search):
    async def candidates(query):
        url = await search(query)
        return [GifCandidate(url, None)] if url else []
    return SimpleNamespace(configured=True, search=search,
                           search_candidates=AsyncMock(side_effect=candidates),
                           download_still=AsyncMock(return_value=None))


class GifDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def test_link_removal_preserves_labels_and_other_urls(self):
        url = "https://giphy.com/gifs/synthetic"
        for text, expected in (
            (f"Synthetic answer\n{url}", "Synthetic answer"),
            (f"Synthetic answer\n<{url}>", "Synthetic answer"),
            (f"[Synthetic label]({url})", "Synthetic label"),
            (f"{url}\n{url}", ""),
            (url + "-other", url + "-other"),
            (url + "?other=1", url + "?other=1"),
            ("https://tenor.com/view/synthetic-other", "https://tenor.com/view/synthetic-other"),
        ):
            with self.subTest(text=text):
                self.assertEqual(_without_reply_gif(text, url), expected)

    async def test_pasted_reply_link_is_removed_before_text_delivery(self):
        url = "https://giphy.com/gifs/synthetic"
        for response, expected in (
            (f"Synthetic answer\n{url}", "Synthetic answer"),
            (url, ""), (f"<{url}>", ""),
        ):
            with self.subTest(response=response):
                context = _context()
                context.state.gif_url = url
                channel = context.source_message.channel
                channel.send = AsyncMock(return_value=SimpleNamespace(id=800))
                message = SimpleNamespace(
                    id=300, channel=channel, archive_reply=False,
                    reply=AsyncMock(return_value=SimpleNamespace(id=800)),
                )
                delivery = AgentDelivery()
                surface = AgentDeliveryMixin()

                async def record_text(message, text, *args, **kwargs):
                    delivery.record(700, text)

                surface._send_response = AsyncMock(side_effect=record_text)
                with (
                    patch("elbow_helper.features.agent.delivery.require_evidence_access",
                          AsyncMock()),
                    patch("elbow_helper.features.agent.delivery.can_show",
                          AsyncMock(return_value=True)),
                ):
                    await surface.send_response(message, response, None, context=context,
                                                delivery=delivery)
                if expected:
                    self.assertEqual(surface._send_response.await_args.args[1], expected)
                    channel.send.assert_awaited_once()
                    self.assertEqual(delivery.text_parts, [expected, url])
                else:
                    surface._send_response.assert_not_awaited()
                    message.reply.assert_awaited_once()
                    self.assertEqual(delivery.text_parts, [url])
                self.assertTrue(delivery.complete)

    async def test_link_is_kept_when_no_separate_gif_is_pending(self):
        context = _context()
        response = "Synthetic answer\nhttps://giphy.com/gifs/synthetic"
        surface = AgentDeliveryMixin()
        surface._send_response = AsyncMock()
        await surface.send_response(object(), response, None, context=context)
        self.assertEqual(surface._send_response.await_args.args[1], response)

    async def test_later_search_replaces_the_retained_gif(self):
        context = _context()
        client = candidate_client(AsyncMock(side_effect=["first", "second"]))
        context = replace(context, bot=SimpleNamespace(gif_client=client))
        await find_gif(context, {"query": "synthetic first"})
        await find_gif(context, {"query": "synthetic second"})
        self.assertEqual(context.state.gif_url, "second")

    async def test_delivery_is_link_only_after_text_or_on_its_own(self):
        for response in ("Synthetic answer", ""):
            context = _context()
            context.state.gif_url = "https://giphy.com/gifs/synthetic"
            sent = SimpleNamespace(id=800)
            channel = context.source_message.channel
            channel.send = AsyncMock(return_value=sent)
            message = SimpleNamespace(id=300, channel=channel, archive_reply=False,
                                      reply=AsyncMock(return_value=sent))
            surface = AgentDeliveryMixin()
            surface._send_response = AsyncMock()
            delivery = AgentDelivery()
            if response:
                delivery.record(700, response)
            conversation = MagicMock()
            surface._conversations = MagicMock()
            with (
                patch("elbow_helper.features.agent.delivery.require_evidence_access", AsyncMock()),
                patch("elbow_helper.features.agent.delivery.can_show",
                      AsyncMock(return_value=True)),
            ):
                await surface.send_response(message, response, None, conversation=conversation,
                                            context=context, delivery=delivery)
            call = (channel.send if response else message.reply).await_args
            self.assertEqual(call.args, (context.state.gif_url,))
            self.assertFalse(call.kwargs["allowed_mentions"].users)
            self.assertFalse(call.kwargs["allowed_mentions"].replied_user)
            self.assertEqual(delivery.text_parts[-1], context.state.gif_url)
            self.assertTrue(delivery.complete)
            surface._conversations.register_reply.assert_called_once_with(conversation, 800)

    async def test_gif_replies_with_optional_text_and_reactions_finish_in_one_model_round(self):
        for react in (False, True):
            for caption in ("", "Synthetic banter."):
                with self.subTest(react=react, caption=caption):
                    context = _context()
                    context.source_message.id = 300
                    context.source_message.add_reaction = AsyncMock()
                    client = candidate_client(AsyncMock(return_value="synthetic-link"))
                    context = replace(context, bot=SimpleNamespace(gif_client=client))
                    registry = build_agent_tools(client)
                    calls = [AgentToolCall("gif", "find_gif", json.dumps({"query": "synthetic"}))]
                    if react:
                        calls.append(AgentToolCall("reaction", "react_to_request", json.dumps({"emoji": "👍"})))
                    step = AgentStep(caption, (plan_call(*calls),), AgentUsage())
                    session = _AgentSession([step])
                    service = AgentService(_AgentModel(session))
                    with patch("elbow_helper.features.agent.engine.service.build_agent_tools",
                               return_value=registry):
                        answer = await service.answer(question="Synthetic request", local_context="",
                                                      context=context)
                    self.assertEqual(answer, caption)
                    self.assertEqual(len(session.calls), 1)
                    self.assertEqual(context.state.gif_url, "synthetic-link")
                    self.assertEqual(context.source_message.add_reaction.await_count, int(react))

    async def test_empty_gif_search_with_a_reaction_still_generates_a_text_reply(self):
        context = _context()
        context.source_message.id = 300
        context.source_message.add_reaction = AsyncMock()
        client = candidate_client(AsyncMock(return_value=None))
        context = replace(context, bot=SimpleNamespace(gif_client=client))
        session = _AgentSession([
            AgentStep("", (plan_call(
                AgentToolCall("reaction", "react_to_request", json.dumps({"emoji": "👍"})),
                AgentToolCall("gif", "find_gif", json.dumps({"query": "synthetic"})),
            ),), AgentUsage()),
            AgentStep("Synthetic text reply.", (), AgentUsage()),
        ])
        answer = await AgentService(_AgentModel(session)).answer(
            question="Synthetic request", local_context="", context=context,
        )
        self.assertEqual(answer, "Synthetic text reply.")
        self.assertEqual(len(session.calls), 2)
        self.assertIsNone(context.state.gif_url)
        context.source_message.add_reaction.assert_awaited_once()

    async def test_plan_searches_replace_in_step_order(self):
        context = _context()
        client = candidate_client(AsyncMock(side_effect=["first", "second"]))
        context = replace(context, bot=SimpleNamespace(gif_client=client))
        session = _AgentSession([
            AgentStep("", (plan_call(
                AgentToolCall("first", "find_gif", json.dumps({"query": "synthetic first"})),
                AgentToolCall("second", "find_gif", json.dumps({"query": "synthetic second"})),
            ),), AgentUsage()),
            AgentStep("Synthetic answer", (), AgentUsage()),
        ])
        await AgentService(_AgentModel(session)).answer(question="Synthetic request",
                                                       local_context="", context=context)
        self.assertEqual(context.state.gif_url, "second")
        self.assertEqual([call.args[0] for call in client.search.await_args_list],
                         ["synthetic first", "synthetic second"])

    async def test_previous_disclosure_applies_to_the_gif_part(self):
        context = _context()
        context.state.gif_url = "https://giphy.com/gifs/synthetic"
        channel = context.source_message.channel
        channel.send = AsyncMock(return_value=SimpleNamespace(id=800))
        message = SimpleNamespace(id=300, channel=channel, archive_reply=False)
        surface = AgentDeliveryMixin()
        surface._send_response = AsyncMock()
        surface._conversations = MagicMock()
        conversation = MagicMock()
        conversation.can_reuse_answer_disclosure.return_value = True
        delivery = AgentDelivery()
        delivery.record(700, "Synthetic answer")
        with (
            patch("elbow_helper.features.agent.delivery.require_evidence_access", AsyncMock()),
            patch("elbow_helper.features.agent.delivery.can_show", AsyncMock(return_value=False)),
        ):
            await surface.send_response(message, "Synthetic answer", None,
                                        conversation=conversation, context=context,
                                        delivery=delivery)
        channel.send.assert_awaited_once()
        self.assertTrue(delivery.complete)

    async def test_interruption_after_text_keeps_delivery_incomplete(self):
        from elbow_helper.features.agent.access import AgentAccessLost
        context = _context()
        context.state.gif_url = "https://giphy.com/gifs/synthetic"
        message = SimpleNamespace(id=300, channel=context.source_message.channel)
        delivery = AgentDelivery()

        async def text_reply(*args, **kwargs):
            delivery.record(700, "Synthetic answer")
            delivery.complete = True

        surface = AgentDeliveryMixin()
        surface._send_response = AsyncMock(side_effect=text_reply)
        with patch("elbow_helper.features.agent.delivery.require_evidence_access",
                   AsyncMock(side_effect=AgentAccessLost("Synthetic loss"))):
            with self.assertRaises(AgentAccessLost):
                await surface.send_response(message, "Synthetic answer", None,
                                            context=context, delivery=delivery)
        self.assertEqual(delivery.message_ids, [700])
        self.assertFalse(delivery.complete)
