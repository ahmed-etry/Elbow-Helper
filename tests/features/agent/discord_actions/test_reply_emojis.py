"""Server shortcodes are rendered before delivery sizing and recording."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.delivery import AgentDeliveryMixin
from elbow_helper.features.agent.discord_actions.reactions import render_server_emojis
from elbow_helper.features.agent.models import AgentDelivery
from features.agent.discord_actions.test_agent_reactions import _server_emoji


class ReplyEmojiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = SimpleNamespace(emojis=(
            _server_emoji("syntheticNod", 101),
            _server_emoji("syntheticWave", 102, animated=True),
            _server_emoji("locked", 103, usable=False),
        ))

    def test_known_shortcodes_render_static_and_animated_markup(self):
        self.assertEqual(
            render_server_emojis(":syntheticNod: :syntheticWave: :syntheticNod:", self.guild),
            "<:syntheticNod:101> <a:syntheticWave:102> <:syntheticNod:101>",
        )

    def test_unknown_unusable_markup_urls_and_code_stay_unchanged(self):
        original = (
            ":unknown: :locked: :syntheticnod: :unknown :locked :syntheticnod "
            "<:syntheticNod:101> <a:syntheticWave:102> 👍 "
            "https://example.invalid/:syntheticNod: `:syntheticNod:`\n"
            "```\n:syntheticWave:\n```"
        )
        self.assertEqual(render_server_emojis(original, self.guild), original)
        self.assertEqual(render_server_emojis(":syntheticNod:", None), ":syntheticNod:")

    def test_known_shortcodes_missing_the_closing_colon_render_in_prose(self):
        self.assertEqual(
            render_server_emojis(":syntheticNod is right. :syntheticWave! :syntheticNod", self.guild),
            "<:syntheticNod:101> is right. <a:syntheticWave:102>! <:syntheticNod:101>",
        )

    def test_incomplete_shortcodes_in_code_urls_and_longer_names_stay_unchanged(self):
        original = (
            "`:syntheticNod` ```\n:syntheticWave\n``` "
            "https://example.invalid/:syntheticNod value:syntheticNod "
            ":syntheticNodExtra :syntheticNod-extra"
        )
        self.assertEqual(render_server_emojis(original, self.guild), original)

    async def test_delivery_renders_the_reported_incomplete_shortcode_form(self):
        surface = AgentDeliveryMixin()
        surface.bot = SimpleNamespace(user=None)
        message = SimpleNamespace(
            id=1, guild=self.guild, mentions=[], archive_reply=False,
            channel=SimpleNamespace(), reply=AsyncMock(return_value=SimpleNamespace(id=2)),
        )
        delivery = AgentDelivery()
        await surface.send_response(message, ":syntheticNod is right.", None, delivery=delivery)
        self.assertEqual(message.reply.await_args.args[0], "<:syntheticNod:101> is right.")
        self.assertEqual(delivery.text_parts, ["<:syntheticNod:101> is right."])

    async def test_delivery_chunks_and_records_rendered_text(self):
        surface = AgentDeliveryMixin()
        surface.bot = SimpleNamespace(user=None)
        message = SimpleNamespace(
            id=1, guild=self.guild, mentions=[], archive_reply=False,
            channel=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=3))),
            reply=AsyncMock(return_value=SimpleNamespace(id=2)),
        )
        delivery = AgentDelivery()
        original = "x" * 1980 + " :syntheticNod: :syntheticWave:"
        await surface.send_response(message, original, None, delivery=delivery)
        rendered = render_server_emojis(original, self.guild)
        self.assertEqual(" ".join(delivery.text_parts), rendered)
        self.assertEqual(delivery.generated_parts, delivery.text_parts)
        self.assertTrue(all(len(part) <= 2000 for part in delivery.text_parts))
        self.assertEqual(message.reply.await_args.args[0], delivery.text_parts[0])
        self.assertEqual(message.channel.send.await_args.args[0], delivery.text_parts[1])
        self.assertTrue(delivery.complete)
