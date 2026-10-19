"""Synthetic animation frames and GIF messages stay bounded and labelled for one animation."""

import io
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, patch

import discord
from PIL import Image

from elbow_helper.configuration.guild import GUILD_ID
from elbow_helper.features.agent.cog import AgentCog
from elbow_helper.features.agent.files.images import request_images
from features.agent.discord_actions.test_gif_choice import png


def animation():
    frames = [Image.new("RGB", (20, 20), (index * 30, 0, 0)) for index in range(8)]
    output = io.BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:],
                   duration=100, loop=0)
    return output.getvalue()


def source(identifier=1, *, content="", embeds=(), attachments=()):
    return SimpleNamespace(id=identifier, content=content, embeds=list(embeds),
                           attachments=list(attachments))


def context(message, replied=None):
    return SimpleNamespace(source_message=message,
                           attachment_sources=(message,) if replied is None else (message, replied),
                           bot=SimpleNamespace(http=SimpleNamespace(get_from_cdn=AsyncMock())))


class GifImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_uploaded_animation_is_four_even_frames_of_one_gif(self):
        data = animation()
        item = SimpleNamespace(filename="synthetic.gif", content_type="image/gif", size=len(data),
                               read=AsyncMock(return_value=data))
        images = await request_images(context(source(attachments=[item])))
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].mime_type, "image/png")
        self.assertIn("Frames of one GIF", images[0].label)
        sheet = Image.open(io.BytesIO(images[0].data))
        for position, original in enumerate((0, 2, 4, 6)):
            self.assertEqual(sheet.getpixel((position % 3 * 240 + 5, position // 3 * 200 + 25)),
                             (original * 30, 0, 0))

    async def test_each_provider_link_resolves_animation_from_its_page(self):
        for provider in ("klipy", "giphy", "tenor"):
            page = f"https://{provider}.com/gifs/synthetic"
            media = f"https://media.{provider}.com/synthetic.gif"
            message = source(content=page)
            selected = context(source(2), message)
            selected.bot.http.get_from_cdn.side_effect = lambda url: (
                f'<meta property="og:image" content="{media}">'.encode() if url == page
                else animation())
            images = await request_images(selected)
            self.assertEqual(len(images), 1)
            self.assertIn("Frames of one GIF", images[0].label)
            self.assertIn("replied-to message", images[0].label)
            self.assertEqual(selected.bot.http.get_from_cdn.await_count, 2)

    async def test_delayed_embed_uses_the_animation_without_fetching_the_page(self):
        page = "https://klipy.com/gifs/synthetic"
        media = "https://static.klipy.com/synthetic.gif"
        embed = discord.Embed(type="gifv", url=page).set_image(url=media)
        message = source(content=page)
        message.channel = SimpleNamespace(fetch_message=AsyncMock(
            return_value=source(content=page, embeds=[embed])))
        selected = context(message)
        selected.bot.http.get_from_cdn.return_value = animation()
        with patch("elbow_helper.features.agent.files.gif_media.asyncio.sleep", AsyncMock()):
            images = await request_images(selected)
        message.channel.fetch_message.assert_awaited_once_with(message.id)
        selected.bot.http.get_from_cdn.assert_awaited_once_with(media)
        self.assertIn("Frames of one GIF", images[0].label)

    async def test_video_only_embed_uses_its_still(self):
        embed = discord.Embed.from_dict({"type": "gifv",
            "video": {"url": "https://media.tenor.com/synthetic.mp4"},
            "thumbnail": {"url": "https://media.tenor.com/synthetic.png"}})
        selected = context(source(embeds=[embed]))
        selected.bot.http.get_from_cdn.return_value = png("red")
        images = await request_images(selected)
        self.assertEqual(images[0].mime_type, "image/png")
        self.assertIn("Still of one GIF", images[0].label)
        selected.bot.http.get_from_cdn.assert_awaited_once_with(
            "https://media.tenor.com/synthetic.png")

    async def test_deadline_and_non_provider_links_do_not_start_downloads(self):
        message = source(content="https://synthetic.invalid/anything.gif")
        selected = context(message)
        self.assertEqual(await request_images(selected), ())
        selected.bot.http.get_from_cdn.assert_not_called()

    async def test_several_gifs_and_missing_embed_metadata_are_supported(self):
        links = [f"https://giphy.com/synthetic-{index}.gif" for index in range(2)]
        selected = context(SimpleNamespace(id=1, content=" ".join(links), attachments=[]))
        selected.bot.http.get_from_cdn.return_value = animation()
        images = await request_images(selected)
        self.assertEqual(len(images), 2)
        self.assertTrue(all("Frames of one GIF" in image.label for image in images))

    async def test_embed_wait_ends_within_the_remaining_request_budget(self):
        message = source(content="https://tenor.com/view/synthetic")
        message.channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
        selected = context(message)
        selected.deadline_monotonic = time.monotonic() + 2.02
        self.assertEqual(await request_images(selected), ())
        message.channel.fetch_message.assert_not_called()
        selected.bot.http.get_from_cdn.assert_not_called()
        message.content = "https://tenor.com/view/synthetic"
        selected.deadline_monotonic = time.monotonic() - 1
        self.assertEqual(await request_images(selected), ())
        selected.bot.http.get_from_cdn.assert_not_called()

    def test_mention_and_reply_with_only_an_image_are_requests(self):
        cog = object.__new__(AgentCog)
        cog.bot = SimpleNamespace(user=SimpleNamespace(id=99))
        cog._reply_conversation = lambda message: object() if message.reference else None
        for content, mentions, reference in (("<@99>", [99], None), ("", [], object())):
            message = source(content=content, attachments=[SimpleNamespace(
                filename="synthetic.png", content_type="image/png")])
            message.raw_mentions = mentions
            message.reference = reference
            message.guild = SimpleNamespace(id=GUILD_ID)
            message.author = SimpleNamespace(bot=False)
            self.assertTrue(cog._is_agent_request(message))
            self.assertEqual(cog._extract_question(message),
                             "Respond to the image, sticker or GIF in this message.")
