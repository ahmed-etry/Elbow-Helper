"""Synthetic media retains its sender, caption and reply recipient for vision."""

import io
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord
from PIL import Image

from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.features.agent.cog import AgentCog
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.files.images import MAX_IMAGE_BYTES, request_images
from elbow_helper.features.agent.text import message_text
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
from features.agent.discord_actions.management_helpers import fixture
from features.agent.files.image_helpers import png
from features.agent.engine.test_agent_service import _AgentModel, _AgentSession, _context
from features.agent.files.test_gif_images import animation


def sticker(format=discord.StickerFormatType.png, data=None):
    return SimpleNamespace(name="Synthetic salute", format=format,
                           read=AsyncMock(return_value=png("red") if data is None else data))


class StickerImageTests(unittest.IsolatedAsyncioTestCase):
    def context(self, item):
        context, channel, _, _ = fixture()
        source = context.source_message
        source.content = "Synthetic caption"
        source.author = context.member
        source.attachments = []
        source.embeds = []
        source.stickers = [item]
        context.bot.user = SimpleNamespace(id=20)
        context.attachment_sources = (source,)
        return context, source, channel

    async def test_static_sticker_reaches_vision_with_sender_caption_and_bot_recipient(self):
        item = sticker()
        context, source, channel = self.context(item)
        target = SimpleNamespace(id=299, channel=channel, author=SimpleNamespace(
            id=20, display_name="Synthetic bot"))
        source.reference = SimpleNamespace(message_id=299, resolved=target)
        images = await request_images(context)
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].data, png("red"))
        self.assertEqual(images[0].mime_type, "image/png")
        self.assertIn('sticker "Synthetic salute"', images[0].label)
        self.assertIn('"sender": {"name": "Synthetic requester", "member_id": 10, "is_you": false}',
                      images[0].label)
        self.assertIn('"message_text": "Synthetic caption"', images[0].label)
        self.assertIn('"reply_recipient": {"name": "Synthetic bot", "member_id": 20, "is_you": true}',
                      images[0].label)
        self.assertEqual(context.state.image_sources, [(channel.id, source.id)])
        self.assertEqual(context.state.source_channels, {channel.id})
        self.assertEqual(message_text(source), "Synthetic caption\n[sticker: Synthetic salute]")

    async def test_attachment_labels_distinguish_human_reply_targets_from_the_bot(self):
        context, source, channel = self.context(sticker())
        source.stickers = []
        source.attachments = [SimpleNamespace(filename="synthetic.png", content_type="image/png",
                                             size=3, read=AsyncMock(return_value=b"png"))]
        target = SimpleNamespace(id=299, channel=channel, author=SimpleNamespace(
            id=40, display_name="Synthetic member"), attachments=[], embeds=[])
        source.reference = SimpleNamespace(message_id=299, resolved=None)
        context.attachment_sources = (source, target)
        channel.fetch_message = AsyncMock(return_value=target)
        images = await request_images(context)
        self.assertIn('"reply_recipient": {"name": "Synthetic member", "member_id": 40, "is_you": false}',
                      images[0].label)
        context.attachment_sources = (source,)
        source.reference.resolved = SimpleNamespace(id=299, channel=SimpleNamespace(id=999),
                                                   author=target.author)
        images = await request_images(context)
        self.assertIn('"replies_to_message_id": 299', images[0].label)
        self.assertNotIn('"reply_recipient"', images[0].label)

    async def test_animated_stickers_become_png_contact_sheets(self):
        output = io.BytesIO()
        frames = [Image.new("RGB", (20, 20), color) for color in ("red", "blue")]
        frames[0].save(output, format="PNG", save_all=True, append_images=frames[1:], duration=100)
        for format, data in ((discord.StickerFormatType.apng, output.getvalue()),
                             (discord.StickerFormatType.gif, animation())):
            with self.subTest(format=format):
                context, _, _ = self.context(sticker(format, data))
                images = await request_images(context)
                self.assertEqual(len(images), 1)
                self.assertEqual(images[0].mime_type, "image/png")
                self.assertIn("Frames of one", images[0].label)
                self.assertIn('sticker "Synthetic salute"', images[0].label)
                with Image.open(io.BytesIO(images[0].data)) as sheet:
                    self.assertGreater(sheet.width, 20)

    async def test_unsupported_failed_or_oversized_stickers_keep_names_without_fake_vision(self):
        for format, data, error in (
            (discord.StickerFormatType.lottie, b"{}", None),
            (discord.StickerFormatType.png, b"x" * (MAX_IMAGE_BYTES + 1), None),
            (discord.StickerFormatType.gif, b"bad animation", None),
            (discord.StickerFormatType.png, b"", OSError("Synthetic failure")),
        ):
            with self.subTest(format=format, error=error):
                item = sticker(format, data)
                item.read.side_effect = error
                context, source, channel = self.context(item)
                self.assertEqual(await request_images(context), ())
                self.assertIn('sticker "Synthetic salute"', context.state.image_source_status[0])
                self.assertIn("could not be viewed", context.state.image_source_status[0])
                self.assertEqual(context.state.image_sources, [(channel.id, source.id)])
                if format == discord.StickerFormatType.lottie:
                    item.read.assert_not_awaited()

    async def test_image_cap_and_read_access_apply_to_stickers(self):
        context, source, channel = self.context(sticker())
        source.stickers = [sticker() for _ in range(5)]
        self.assertEqual(len(await request_images(context)), 4)
        source.stickers[-1].read.assert_not_awaited()
        for item in source.stickers:
            item.read.reset_mock()
        channel.permissions_for = lambda _: discord.Permissions.none()
        self.assertEqual(await request_images(context), ())
        for item in source.stickers:
            item.read.assert_not_awaited()

    async def test_normal_service_passes_sticker_bytes_and_labels_to_the_model(self):
        context = _context()
        source = context.source_message
        source.id, source.content, source.author = 300, "", context.member
        source.stickers = [sticker()]
        context = replace(context, attachment_sources=(source,))
        model = _AgentModel(_AgentSession([AgentStep("Synthetic reply", (), AgentUsage())]))
        await AgentService(model).answer(question="Respond to this sticker", local_context="", context=context)
        self.assertEqual(len(model.request["images"]), 1)
        self.assertEqual(model.request["images"][0].data, png("red"))
        self.assertIn('sticker "Synthetic salute"', model.request["prompt"])
        self.assertIn('"sender"', model.request["prompt"])

    def test_mention_and_reply_with_only_a_sticker_are_requests(self):
        cog = object.__new__(AgentCog)
        cog.bot = SimpleNamespace(user=SimpleNamespace(id=99))
        cog._reply_conversation = lambda message: object() if message.reference else None
        for content, mentions, reference in (("<@99>", [99], None), ("", [], object())):
            message = SimpleNamespace(content=content, raw_mentions=mentions, reference=reference,
                                      guild=SimpleNamespace(id=GUILD_ID), author=SimpleNamespace(bot=False),
                                      stickers=[sticker()])
            self.assertTrue(cog._is_agent_request(message))
            self.assertEqual(cog._extract_question(message),
                             "Respond to the image, sticker or GIF in this message.")
