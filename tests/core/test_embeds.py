from datetime import datetime, timezone
import unittest

import discord

from elbow_helper.infrastructure.discord.embeds import stable_embed_payload


class BoardEmbedTests(unittest.TestCase):
    def test_discord_metadata_does_not_trigger_board_edits(self):
        original = discord.Embed(title="Synthetic board").set_image(url="https://example.invalid/board.png")
        received = discord.Embed.from_dict({
            **original.to_dict(), "timestamp": datetime.now(timezone.utc).isoformat(),
            "image": {"url": "https://example.invalid/board.png", "proxy_url": "https://example.invalid/proxy.png",
                      "height": 50, "width": 100},
        })
        self.assertEqual(stable_embed_payload(original), stable_embed_payload(received))
        received.title = "Changed board"
        self.assertNotEqual(stable_embed_payload(original), stable_embed_payload(received))

    def test_empty_embeds_and_changed_media_keep_their_meaning(self):
        self.assertEqual(stable_embed_payload(discord.Embed()), discord.Embed().to_dict())
        old = discord.Embed().set_thumbnail(url="https://example.invalid/old.png")
        new = discord.Embed().set_thumbnail(url="https://example.invalid/new.png")
        self.assertNotEqual(stable_embed_payload(old), stable_embed_payload(new))
