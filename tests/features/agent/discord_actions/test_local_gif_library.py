"""Exercise curated attachments using temporary synthetic media only."""

from dataclasses import replace
from pathlib import Path
import shutil
from uuid import uuid4
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.core.paths import ApplicationPaths
from elbow_helper.features.agent.delivery import AgentDeliveryMixin
from elbow_helper.features.agent.discord_actions.gif_library import ITEM_BY_ID, channel_gif_urls
from elbow_helper.features.agent.discord_actions.gifs import channel_gif_history, find_gif, gif_tools
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.models import AgentDelivery
from features.agent.engine.helpers import social_step
from features.agent.engine.test_agent_service import _AgentModel, _AgentSession
from features.agent.engine.test_banter_gifs import gif_context


class LocalGifLibraryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = Path.cwd() / ('gif-test-' + uuid4().hex)
        temporary.mkdir()
        self.addCleanup(shutil.rmtree, temporary)
        paths = ApplicationPaths.from_project_root(temporary)
        self.directory = paths.data_root / "agent" / "gifs"
        self.directory.mkdir(parents=True)
        self.context = replace(gif_context(), bot=SimpleNamespace(paths=paths))
        self.context.guild.filesize_limit = 16
        self.context.source_message.id = 300
        self.context.source_message.archive_reply = False
        self.context.source_message.reply = AsyncMock(return_value=SimpleNamespace(id=301))
        self.context.source_message.channel.send = AsyncMock(return_value=SimpleNamespace(id=302))

    def library(self, entries):
        return patch.dict(ITEM_BY_ID, {
            'synthetic' + (f'_{i + 1}' if i else ''): {
                'id': 'synthetic' + (f'_{i + 1}' if i else ''), 'media': media,
                'use': 'Synthetic reaction', 'shows': 'Synthetic image', 'group': 'win',
            } for i, media in enumerate(entries)
        })

    async def test_all_local_formats_work_without_a_search_client_or_cooldown(self):
        for extension in ("gif", "png", "jpg", "webp"):
            name = "synthetic." + extension
            (self.directory / name).write_bytes(b"synthetic media")
            with self.library([name]):
                self.assertTrue(gif_tools())
                result = await find_gif(self.context, {"query": "synthetic"})
            self.assertEqual(result, {"url": "attachment://" + name})
            self.assertEqual(self.context.state.gif_attachment.data, b"synthetic media")
            row = SimpleNamespace(author=SimpleNamespace(id=999), content="",
                                  attachments=[SimpleNamespace(filename=name)])
            self.assertEqual(channel_gif_history([row], 999), (True,))
            self.assertIn("attachment://" + name, channel_gif_urls([row]))
            self.context.state.channel_gif_urls = channel_gif_urls([row])
            self.context.state.gif_url = None
            with self.library([name]):
                self.assertIn('error', await find_gif(self.context, {"query": "synthetic"}))
                self.assertIsNone(self.context.state.gif_url)
            self.context.state.channel_gif_urls.clear()

    async def test_missing_empty_oversized_and_unreadable_files_log_and_leave_text(self):
        for name, data in (("missing.gif", None), ("empty.png", b""), ("huge.jpg", b"x" * 17)):
            if data is not None:
                (self.directory / name).write_bytes(data)
            with self.library([name]), self.assertLogs(
                "elbow_helper.features.agent.discord_actions.gif_library", level="WARNING",
            ):
                answer = await AgentService(_AgentModel(
                    _AgentSession([social_step("Synthetic fallback.", meme_id="synthetic")]), route="social",
                )).answer(question="Synthetic exchange", local_context="", context=self.context)
            self.assertEqual(answer, "Synthetic fallback.")
            self.assertIsNone(self.context.state.gif_url)
        (self.directory / "denied.gif").write_bytes(b"synthetic")
        with (self.library(["denied.gif"]), patch.object(Path, "open", side_effect=PermissionError("Synthetic denial")),
              self.assertLogs("elbow_helper.features.agent.discord_actions.gif_library", level="WARNING")):
            self.assertIn("error", await find_gif(self.context, {"query": "synthetic"}))

    async def test_missing_chosen_file_and_unsafe_paths_do_not_select_a_different_clip(self):
        (self.directory / "good.gif").write_bytes(b"synthetic")
        with self.library(["missing.gif", "good.gif"]), self.assertLogs(
                "elbow_helper.features.agent.discord_actions.gif_library", level="WARNING"):
            self.assertIn('error', await find_gif(self.context, {"query": "synthetic"}))
        for media in ("../outside.gif", "D:\\outside.gif", "script.exe", "https://example.com/file.gif",
                      "https://klipy.com.example.com/file.gif", "https://fakeklipy.com/file.gif",
                      "http://static2.klipy.com/file.gif", "https://["):
            with self.library([media]):
                self.assertIn('error', await find_gif(self.context, {"query": "synthetic"}))
        self.assertIsNone(self.context.state.gif_attachment)

    async def test_approved_links_still_work_and_clear_a_prior_attachment(self):
        (self.directory / "good.gif").write_bytes(b"synthetic")
        with self.library(["good.gif"]):
            await find_gif(self.context, {"query": "synthetic"})
        for link in ("https://tenor.com/view/synthetic-123",
                     "https://klipy.com/gifs/synthetic", "https://static2.klipy.com/synthetic.gif"):
            with self.library([link]):
                self.assertEqual(await find_gif(self.context, {"query": "synthetic"}), {"url": link})
            self.assertIsNone(self.context.state.gif_attachment)

    async def test_local_social_reply_ignores_legacy_caption_flag(self):
        name = "synthetic.png"
        (self.directory / name).write_bytes(b"synthetic media")
        for with_text in (False, True):
            self.context.source_message.reply.reset_mock()
            self.context.source_message.channel.send.reset_mock()
            with self.library([name]):
                session = _AgentSession([
                    social_step("Synthetic line.", meme_id="synthetic", gif_with_text=with_text),
                ])
                answer = await AgentService(_AgentModel(session, route="social")).answer(
                    question="Synthetic exchange", local_context="", context=self.context,
                )
            self.assertEqual(answer, "")
            self.assertEqual(self.context.state.gif_fallback_text, "Synthetic line.")
            delivery = AgentDelivery()
            surface = AgentDeliveryMixin()
            surface._send_response = AsyncMock()
            with (patch("elbow_helper.features.agent.delivery.require_evidence_access", AsyncMock()),
                  patch("elbow_helper.features.agent.delivery.assert_answer_scope", AsyncMock())):
                await surface.send_response(self.context.source_message, answer, None,
                                            context=self.context, delivery=delivery)
            sent = self.context.source_message.reply
            self.assertEqual(sent.await_args.args, (None,))
            file = sent.await_args.kwargs["file"]
            self.assertEqual(file.filename, name)
            self.assertTrue(file.fp.closed)
            self.assertFalse(sent.await_args.kwargs["allowed_mentions"].users)
            self.assertEqual(delivery.text_parts[-1], "attachment://" + name)
            self.assertTrue(delivery.complete)
            self.assertEqual(len(session.calls), 1)

    async def test_changed_upload_limit_restores_fallback_text_before_delivery(self):
        (self.directory / "synthetic.gif").write_bytes(b"synthetic media")
        with self.library(["synthetic.gif"]):
            answer = await AgentService(_AgentModel(
                _AgentSession([social_step("Synthetic fallback.", meme_id="synthetic")]), route="social",
            )).answer(question="Synthetic exchange", local_context="", context=self.context)
        self.assertEqual(answer, "")
        self.context.guild.filesize_limit = 1
        surface = AgentDeliveryMixin()
        surface._send_response = AsyncMock()
        with self.assertLogs("elbow_helper.features.agent.delivery", level="WARNING"):
            await surface.send_response(self.context.source_message, answer, None, context=self.context)
        self.assertEqual(surface._send_response.await_args.args[1], "Synthetic fallback.")
        self.context.source_message.reply.assert_not_awaited()
