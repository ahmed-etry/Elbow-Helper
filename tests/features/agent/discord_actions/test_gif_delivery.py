"""Synthetic GIF replies retain one link and use separate tracked Discord messages."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.features.agent.delivery import AgentDeliveryMixin, _without_reply_gif
from elbow_helper.features.agent.discord_actions.gifs import find_gif
from elbow_helper.features.agent.discord_actions.gif_library import ITEM_BY_ID
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.models import AgentDelivery
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage
from features.agent.engine.test_agent_service import _AgentModel, _AgentSession, _context
from tests.features.agent_plan_helpers import plan_call
from features.agent.engine.helpers import social_step
from features.agent.engine.test_banter_gifs import gif_context


class GifDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def test_link_removal_preserves_labels_and_other_urls(self):
        url = "https://tenor.com/view/synthetic"
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
        url = "https://tenor.com/view/synthetic"
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
                    patch("elbow_helper.features.agent.delivery.assert_answer_scope",
                          AsyncMock(return_value=True)),
                ):
                    await surface.send_response(message, response, None, context=context,
                                                delivery=delivery)
                surface._send_response.assert_not_awaited()
                message.reply.assert_awaited_once()
                self.assertEqual(delivery.text_parts, [url])
                self.assertEqual(context.state.gif_fallback_text, expected)
                self.assertTrue(delivery.complete)

    async def test_link_is_kept_when_no_separate_gif_is_pending(self):
        context = _context()
        response = "Synthetic answer\nhttps://tenor.com/view/synthetic"
        surface = AgentDeliveryMixin()
        surface._send_response = AsyncMock()
        await surface.send_response(object(), response, None, context=context)
        self.assertEqual(surface._send_response.await_args.args[1], "https://tenor.com/view/synthetic")

    async def test_later_library_selection_replaces_the_retained_gif(self):
        context = _context()
        clips = {key: {'id': key, 'media': f'https://tenor.com/view/{key}'}
                 for key in ('first', 'second')}
        with patch.dict(ITEM_BY_ID, clips, clear=True):
            await find_gif(context, {'query': 'first'})
            await find_gif(context, {'query': 'second'})
        self.assertEqual(context.state.gif_url, clips['second']['media'])

    async def test_delivery_is_link_only_after_text_or_on_its_own(self):
        for response in ("Synthetic answer", ""):
            context = _context()
            context.state.gif_url = "https://tenor.com/view/synthetic"
            sent = SimpleNamespace(id=800)
            channel = context.source_message.channel
            channel.send = AsyncMock(return_value=sent)
            message = SimpleNamespace(id=300, channel=channel, archive_reply=False,
                                      reply=AsyncMock(return_value=sent))
            surface = AgentDeliveryMixin()
            surface._send_response = AsyncMock()
            delivery = AgentDelivery()
            conversation = MagicMock()
            surface._conversations = MagicMock()
            with (
                patch("elbow_helper.features.agent.delivery.require_evidence_access", AsyncMock()),
                patch("elbow_helper.features.agent.delivery.assert_answer_scope",
                      AsyncMock(return_value=True)),
            ):
                await surface.send_response(message, response, None, conversation=conversation,
                                            context=context, delivery=delivery)
            call = message.reply.await_args
            self.assertEqual(call.args, (context.state.gif_url,))
            self.assertFalse(call.kwargs["allowed_mentions"].users)
            self.assertFalse(call.kwargs["allowed_mentions"].replied_user)
            self.assertEqual(delivery.text_parts[-1], context.state.gif_url)
            self.assertTrue(delivery.complete)
            surface._conversations.register_reply.assert_called_once_with(conversation, 800)

    async def test_gif_replies_ignore_legacy_caption_flag_without_another_round(self):
        for react in (False, True):
            for caption in ("", "Synthetic banter."):
                with self.subTest(react=react, caption=caption):
                    context = _context()
                    context.source_message.id = 300
                    context.source_message.add_reaction = AsyncMock()

                    context = replace(context, bot=SimpleNamespace())
                    context = gif_context(context)
                    step = social_step(caption or "Synthetic fallback.",
                        meme_id="synthetic", gif_with_text=bool(caption),
                        form="text_reactions" if react else "text",
                        reactions=["\U0001f44d"] if react else [])
                    session = _AgentSession([step, AgentStep(caption, (), AgentUsage())])
                    service = AgentService(_AgentModel(session, route="social"))
                    with patch.dict(ITEM_BY_ID, synthetic={'id': 'synthetic', 'media': 'https://tenor.com/view/synthetic-123',
                            'use': 'Synthetic reaction', 'shows': 'Synthetic image', 'group': 'win'}):
                        answer = await service.answer(question="Synthetic request", local_context="", context=context)
                    self.assertEqual(answer, "")
                    self.assertEqual(context.state.gif_fallback_text, caption or "Synthetic fallback.")
                    self.assertEqual(len(session.calls), 1)
                    self.assertEqual(context.state.gif_url, "https://tenor.com/view/synthetic-123")
                    self.assertEqual(context.source_message.add_reaction.await_count, int(react))

    async def test_no_meme_with_a_reaction_still_generates_a_text_reply(self):
        context = _context()
        context.source_message.id = 300
        context.source_message.add_reaction = AsyncMock()

        context = replace(context, bot=SimpleNamespace())
        context = gif_context(context)
        session = _AgentSession([social_step("Synthetic text reply.", form="text_reactions",
                                            reactions=["\U0001f44d"]),
                                 AgentStep("Synthetic text reply.", (), AgentUsage())])
        answer = await AgentService(_AgentModel(session, route="social")).answer(
            question="Synthetic request", local_context="", context=context,
        )
        self.assertEqual(answer, "Synthetic text reply.")
        self.assertEqual(len(session.calls), 1)
        self.assertIsNone(context.state.gif_url)
        context.source_message.add_reaction.assert_awaited_once()

    async def test_research_plan_cannot_search_for_a_reply_gif(self):
        context = _context()

        context = replace(gif_context(context), bot=SimpleNamespace())
        session = _AgentSession([
            AgentStep("", (plan_call(AgentToolCall("gif", "find_gif", '{"query":"synthetic"}')),), AgentUsage()),
            AgentStep("Synthetic answer", (), AgentUsage()),
        ])
        answer = await AgentService(_AgentModel(session)).answer(question="Synthetic request",
                                                               local_context="", context=context)
        self.assertEqual(answer, "Synthetic answer")
        self.assertIsNone(context.state.gif_url)


    async def test_access_loss_before_media_sends_no_text_and_keeps_delivery_incomplete(self):
        from elbow_helper.features.agent.access import AgentAccessLost
        context = _context()
        context.state.gif_url = "https://tenor.com/view/synthetic"
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
        self.assertEqual(delivery.message_ids, [])
        surface._send_response.assert_not_awaited()
        self.assertFalse(delivery.complete)
