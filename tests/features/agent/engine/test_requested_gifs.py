"""Requested library media through the real social plan and delivery path."""

from dataclasses import replace
from pathlib import Path
import shutil
from uuid import uuid4
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.core.paths import ApplicationPaths
from elbow_helper.features.agent.delivery import AgentDeliveryMixin
from elbow_helper.features.agent.discord_actions.gif_library import ITEM_BY_ID
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.engine.social_reply import (
    MEME_SEND_FIT, salvage_choice, social_plan,
)
from elbow_helper.features.agent.models import AgentDelivery
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
from features.agent.engine.helpers import social_step
from features.agent.engine.test_agent_service import _AgentModel, _AgentSession
from features.agent.engine.test_banter_gifs import gif_context


class RequestedGifTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = Path.cwd() / ('gif-test-' + uuid4().hex)
        temporary.mkdir()
        self.addCleanup(shutil.rmtree, temporary)
        paths = ApplicationPaths.from_project_root(temporary)
        self.directory = paths.data_root / "agent" / "gifs"
        self.directory.mkdir(parents=True)
        self.client = SimpleNamespace(configured=True, search_candidates=AsyncMock())
        self.context = replace(gif_context(recent=(True,)), bot=SimpleNamespace(
            paths=paths, gif_client=self.client, agent_model=SimpleNamespace(complete=AsyncMock())))
        self.context.source_message.id = 300
        self.context.source_message.archive_reply = False
        self.context.source_message.reply = AsyncMock(return_value=SimpleNamespace(id=301))
        self.context.source_message.channel.send = AsyncMock(return_value=SimpleNamespace(id=302))

    def library(self, entries):
        items = {
            'synthetic' + (f'_{i + 1}' if i else ''): {
                'id': 'synthetic' + (f'_{i + 1}' if i else ''), 'media': media,
                'use': 'Synthetic reaction', 'shows': 'Synthetic image', 'group': 'win',
            } for i, media in enumerate(entries)
        }
        if entries:
            items.update({key: ITEM_BY_ID[key] for key in list(ITEM_BY_ID)[:3]})
        return patch.dict(ITEM_BY_ID, items, clear=True)

    async def answer(self, question, *, requested=True, query="synthetic", with_text=False, **extra):
        session = _AgentSession([social_step(
            "Synthetic fallback.", meme_id=query, gif_requested=requested,
            **({"meme_shortlist": []} if query is None else {}),
            gif_with_text=with_text, **extra), AgentStep("Synthetic fallback.", (), AgentUsage())])
        model = _AgentModel(session, route="social")
        answer = await AgentService(model).answer(
            question=question, local_context="Synthetic social exchange.", context=self.context)
        self.assertEqual(len(session.calls), 1)
        self.client.search_candidates.assert_not_awaited()
        self.context.bot.agent_model.complete.assert_not_awaited()
        return answer, model

    async def test_short_meme_and_mood_requests_deliver_with_recent_images_or_unknown_history(self):
        name = "synthetic.gif"
        (self.directory / name).write_bytes(b"synthetic media")
        for question in ("@bot moon dance", "@bot inner peace", "Send a celebration GIF"):
            for history in ((True,), None):
                for with_text in (False, True):
                    with self.subTest(question=question, history=history, with_text=with_text):
                        self.context.state.channel_bot_gifs = history
                        self.context.source_message.reply.reset_mock()
                        self.context.source_message.channel.send.reset_mock()
                        with self.library([name]):
                            answer, model = await self.answer(question, with_text=with_text)
                        self.assertEqual(answer, "")
                        self.assertEqual(self.context.state.gif_fallback_text, "Synthetic fallback.")
                        self.assertEqual(self.context.state.gif_url, "attachment://" + name)
                        self.assertEqual(self.context.state.channel_bot_gifs, history)
                        schema = model.request["tools"][0].parameters
                        self.assertIn("gif_requested", schema["required"])
                        self.assertIn("The three library memes closest to this moment",
                                      schema["properties"]["meme_shortlist"]["description"])
                        delivery = AgentDelivery()
                        surface = AgentDeliveryMixin()
                        surface._send_response = AsyncMock()
                        with (patch("elbow_helper.features.agent.delivery.require_evidence_access", AsyncMock()),
                              patch("elbow_helper.features.agent.delivery.assert_answer_scope", AsyncMock())):
                            await surface.send_response(self.context.source_message, answer, None,
                                                        context=self.context, delivery=delivery)
                        sent = self.context.source_message.reply
                        self.assertEqual(sent.await_args.kwargs["file"].filename, name)
                        self.assertTrue(delivery.complete)
                        self.assertEqual(delivery.text_parts[-1], "attachment://" + name)

    async def test_member_words_do_not_override_the_model_image_choice(self):
        (self.directory / "synthetic.gif").write_bytes(b"synthetic")
        with self.library(["synthetic.gif"]):
            answer, _ = await self.answer("Please send a GIF", requested=False, query=None)
            self.assertEqual(answer, "Synthetic fallback.")
            self.assertIsNone(self.context.state.gif_url)
            answer, _ = await self.answer("zorp", requested=True)
            self.assertEqual(answer, "")
            self.assertEqual(self.context.state.gif_url, "attachment://synthetic.gif")

    async def test_no_valid_shortlist_keeps_text_for_requested_and_optional_media(self):
        for requested in (False, True):
            answer, _ = await self.answer('Synthetic exchange', requested=requested, query=None)
            self.assertEqual(answer, 'Synthetic fallback.')
            self.assertIsNone(self.context.state.gif_url)

    async def test_invalid_shortlist_empty_or_unreadable_library_keeps_social_text_without_search(self):
        for history in ((), (True,), None):
            for entries, query in (([], "synthetic"), (["missing.gif"], "synthetic"),
                                   (["synthetic.gif"], None)):
                with self.subTest(history=history, entries=entries, query=query):
                    self.context.state.channel_bot_gifs = history
                    (self.directory / "synthetic.gif").write_bytes(b"synthetic")
                    with self.library(entries):
                        answer, _ = await self.answer("Show me a meme", query=query)
                    self.assertEqual(answer, "Synthetic fallback.")
                    self.assertIsNone(self.context.state.gif_url)

    async def test_completely_empty_library_without_provider_keeps_social_text(self):
        self.context.bot.gif_client = None
        with patch.dict(ITEM_BY_ID, {}, clear=True):
            answer, _ = await self.answer("inner peace", query=None)
        self.assertEqual(answer, "Synthetic fallback.")

    async def test_invalid_shortlist_preserves_the_chosen_social_reaction(self):
        self.context.source_message.add_reaction = AsyncMock()
        with self.library(["unselected.gif"]):
            answer, _ = await self.answer("moon dance", query=None,
                form="text_reactions", reactions=("\U0001f680",))
        self.assertEqual(answer, "Synthetic fallback.")
        self.context.source_message.add_reaction.assert_awaited_once_with("\U0001f680")
        self.assertIsNone(self.context.state.gif_url)

    async def test_request_prefers_fresh_media_but_can_reuse_seen_media(self):
        for name in ("seen.gif", "fresh.gif"):
            (self.directory / name).write_bytes(b"synthetic")
        self.context.state.channel_gif_ids.add("attachment://seen.gif")
        with self.library(["seen.gif", "fresh.gif"]):
            answer, _ = await self.answer("moon dance")
            self.assertEqual(answer, "")
            self.assertEqual(self.context.state.gif_attachment.filename, "fresh.gif")
            self.context.state.channel_gif_ids.add("attachment://fresh.gif")
            answer, _ = await self.answer("moon dance")
            self.assertEqual(answer, "")
            self.assertIn(self.context.state.gif_attachment.filename, ("seen.gif", "fresh.gif"))

    async def test_missing_fresh_media_does_not_hide_readable_seen_media(self):
        (self.directory / "seen.gif").write_bytes(b"synthetic")
        self.context.state.channel_gif_ids.add("attachment://seen.gif")
        with self.library(["seen.gif", "missing.gif"]):
            answer, _ = await self.answer("moon dance")
        self.assertEqual(answer, "")
        self.assertEqual(self.context.state.gif_attachment.filename, "seen.gif")

    async def test_requested_repeat_reuses_chosen_item_when_no_unused_clip_exists(self):
        (self.directory / "synthetic.gif").write_bytes(b"synthetic")
        self.context.state.used_gif_urls.add("attachment://synthetic.gif")
        with self.library(["synthetic.gif"]):
            answer, _ = await self.answer("moon dance")
        self.assertEqual(answer, "")
        self.assertEqual(self.context.state.gif_url, "attachment://synthetic.gif")

    async def test_fit_below_the_send_bar_sends_the_line(self):
        (self.directory / "synthetic.gif").write_bytes(b"synthetic")
        with self.library(["synthetic.gif"]):
            answer, _ = await self.answer("Synthetic exchange", requested=False, fit=MEME_SEND_FIT - 1)
        self.assertEqual(answer, "Synthetic fallback.")
        self.assertIsNone(self.context.state.gif_url)
        self.assertIsNone(self.context.state.gif_attachment)

    async def test_unrequested_repeat_without_unused_clip_sends_the_line(self):
        (self.directory / "synthetic.gif").write_bytes(b"synthetic")
        for seen in (self.context.state.channel_gif_ids, self.context.state.used_gif_urls):
            with self.subTest(seen_in_channel=seen is self.context.state.channel_gif_ids):
                seen.add("attachment://synthetic.gif")
                with self.library(["synthetic.gif"]):
                    answer, _ = await self.answer("Synthetic exchange", requested=False, fit=5)
                self.assertEqual(answer, "Synthetic fallback.")
                self.assertIsNone(self.context.state.gif_url)
                self.assertIsNone(self.context.state.gif_attachment)
                seen.clear()

    async def test_unrequested_repeat_uses_unused_clip_of_the_same_joke(self):
        for name in ("seen.gif", "fresh.gif"):
            (self.directory / name).write_bytes(b"synthetic")
        self.context.state.channel_gif_ids.add("attachment://seen.gif")
        with self.library(["seen.gif", "fresh.gif"]):
            answer, _ = await self.answer("Synthetic exchange", requested=False, fit=5)
        self.assertEqual(answer, "")
        self.assertEqual(self.context.state.gif_url, "attachment://fresh.gif")

    async def test_previous_request_does_not_allow_an_unrequested_repeat(self):
        (self.directory / "synthetic.gif").write_bytes(b"synthetic")
        self.context.state.gif_requested = True
        self.context.state.channel_gif_ids.add("attachment://synthetic.gif")
        with self.library(["synthetic.gif"]):
            answer, _ = await self.answer("Synthetic exchange", requested=False, fit=5)
        self.assertEqual(answer, "Synthetic fallback.")
        self.assertFalse(self.context.state.gif_requested)
        self.assertIsNone(self.context.state.gif_url)

    async def test_member_clarification_still_precedes_requested_media(self):
        (self.directory / "synthetic.gif").write_bytes(b"synthetic")
        with (self.library(["synthetic.gif"]), patch(
            "elbow_helper.features.agent.engine.social_reply.social_member_identity",
            AsyncMock(return_value={"status": "needs_input", "missing": ["Which member?"]}),
        )):
            answer, _ = await self.answer("Show a meme for Alex", member_query="Alex")
        self.assertEqual(answer, "Which member?")
        self.assertIsNone(self.context.state.gif_url)

    async def test_identity_rewrite_cannot_retain_an_earlier_requested_bypass(self):
        (self.directory / "synthetic.gif").write_bytes(b"synthetic")
        session = _AgentSession([
            social_step("Synthetic initial line.", gif_requested=True, meme_id="synthetic", member_query="Alex"),
            social_step("Synthetic revised line.", gif_requested=False, meme_id=None, member_query="Alex"),
            AgentStep("Synthetic revised line.", (), AgentUsage()),
        ])
        with (self.library(["synthetic.gif"]), patch(
            "elbow_helper.features.agent.engine.social_reply.social_member_identity",
            AsyncMock(return_value={"status": "resolved", "instruction": "Compose for the resolved member."}),
        )):
            answer = await AgentService(_AgentModel(session, route="social")).answer(
                question="Show a meme for Alex", local_context="", context=self.context)
        self.assertEqual(answer, "Synthetic revised line.")
        self.assertEqual(len(session.calls), 2)
        self.assertIsNone(self.context.state.gif_url)
        self.client.search_candidates.assert_not_awaited()

    def test_invalid_request_flag_cannot_enable_low_fit_media(self):
        import json
        original = json.loads(social_step(gif_requested=True, meme_id="ts_lowkey_tuff", fit=2).tool_calls[0].arguments)
        for changed in ({"gif_requested": "true"}, {"gif_requested": 1}):
            choice = salvage_choice({**original, **changed}, {})
            self.assertFalse(choice["gif_requested"])
            self.assertFalse(social_plan(choice, "invalid", gif_available=True)["steps"])
        choice = salvage_choice({**original, "reply_form": "invalid"}, {})
        self.assertTrue(choice["gif_requested"])
        self.assertEqual(social_plan(choice, "salvaged", gif_available=True)["steps"][0]["arguments"],
                         {"query": "ts_lowkey_tuff"})
        original.pop("gif_requested")
        self.assertFalse(salvage_choice(original, {})["gif_requested"])

