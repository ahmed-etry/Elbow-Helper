"""Server shortcodes are rendered before delivery sizing and recording."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.agent.conversation.delivery import AgentDeliveryMixin
from elbow_helper.agent.discord_actions.reactions import render_server_emojis
from elbow_helper.agent.engine.models import AgentDelivery
from tests.agent.discord_actions.test_agent_reactions import _server_emoji


class ReplyEmojiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = SimpleNamespace(emojis=(
            _server_emoji("syntheticNod", 101),
            _server_emoji("syntheticWave", 102, animated=True),
            _server_emoji("locked", 103, usable=False),
        ))

    def test_known_shortcodes_render_complete_casefolded_and_incomplete_forms(self):
        cases = (
            (":syntheticNod: :syntheticWave: :syntheticNod:", "<:syntheticNod:101> <a:syntheticWave:102> <:syntheticNod:101>"),
            (":syntheticnod: :SYNTHETICNOD", "<:syntheticNod:101> <:syntheticNod:101>"),
            (":syntheticNod is right. :syntheticWave! :syntheticNod", "<:syntheticNod:101> is right. <a:syntheticWave:102>! <:syntheticNod:101>"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(render_server_emojis(text, self.guild), expected)


    def test_unknown_unusable_markup_urls_and_code_stay_unchanged(self):
        original = (
            ":unknown: :locked: :unknown :locked "
            "<:syntheticNod:101> <a:syntheticWave:102> 👍 "
            "https://example.invalid/:syntheticNod: `:syntheticNod:`\n"
            "```\n:syntheticWave:\n```"
        )
        self.assertEqual(render_server_emojis(original, self.guild), original)
        self.assertEqual(render_server_emojis(":syntheticNod:", None), ":syntheticNod:")


    def test_incomplete_shortcodes_in_code_urls_and_longer_names_stay_unchanged(self):
        original = (
            "`:syntheticNod` ```\n:syntheticWave\n``` "
            "https://example.invalid/:syntheticNod value:syntheticNod "
            ":syntheticNodExtra :syntheticNod-extra"
        )
        self.assertEqual(render_server_emojis(original, self.guild), original)


    def test_emoji_layout_preserves_structured_text_code_and_unknown_names(self):
        for original in (":syntheticNod:", "Synthetic answer.\n:unknown:",
                         "Synthetic answer.\nMore prose :syntheticNod:",
                         "```\n<:syntheticNod:101>", "~~~\n<:syntheticNod:101>",
                         "```\ncode\n```\n<:syntheticNod:101>",
                         "> Quoted text\n<:syntheticNod:101>",
                         "| Table |\n<:syntheticNod:101>",
                         "- List item\n<:syntheticNod:101>"):
            with self.subTest(text=original):
                expected = original.replace(":syntheticNod:", "<:syntheticNod:101>") if "<:" not in original else original
                self.assertEqual(render_server_emojis(original, self.guild), expected)

    async def test_delivery_records_rendered_complete_and_incomplete_shortcodes(self):
        for text, expected in (("Synthetic answer.\n\n:syntheticNod:", "Synthetic answer. <:syntheticNod:101>"),
                               (":syntheticNod is right.", "<:syntheticNod:101> is right.")):
            with self.subTest(text=text):
                surface = AgentDeliveryMixin()
                surface.bot = SimpleNamespace(user=None)
                message = SimpleNamespace(id=1, guild=self.guild, mentions=[], archive_reply=False,
                    channel=SimpleNamespace(), reply=AsyncMock(return_value=SimpleNamespace(id=2)))
                delivery = AgentDelivery()
                await surface.send_response(message, text, None, delivery=delivery)
                self.assertEqual(message.reply.await_args.args[0], expected)
                self.assertEqual(delivery.text_parts, [expected])


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
