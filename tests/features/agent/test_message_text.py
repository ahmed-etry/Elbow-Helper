"""Synthetic bot embeds expose all text and resolve mentions consistently."""

from types import SimpleNamespace
import unittest

import discord

from elbow_helper.features.agent.models import AgentIdentity
from elbow_helper.features.agent.text import message_text


class MessageTextTests(unittest.TestCase):
    def test_author_footer_and_existing_parts_resolve_member_mentions(self):
        member = SimpleNamespace(id=7, display_name="Synthetic Member")
        embed = discord.Embed(title="Synthetic log", description="Details for <@7>")
        embed.set_author(name="Subject <@!7>")
        embed.add_field(name="Member", value="<@7>")
        embed.set_footer(text="Recorded for <@7>")
        message = SimpleNamespace(
            content="Entry for <@7>", embeds=[embed], mentions=[],
            attachments=[SimpleNamespace(filename="synthetic.html")],
            guild=SimpleNamespace(get_member=lambda member_id: member if member_id == 7 else None),
        )
        text = message_text(message)
        self.assertIn("Subject @Synthetic Member (member_id=7)", text)
        self.assertIn("Recorded for @Synthetic Member (member_id=7)", text)
        self.assertIn("Member: @Synthetic Member (member_id=7)", text)
        self.assertIn("Entry for @Synthetic Member (member_id=7)", text)
        self.assertIn("Details for @Synthetic Member (member_id=7)", text)
        self.assertIn("synthetic.html", text)
        self.assertNotIn("<@", text)

    def test_agent_identity_is_preserved_in_all_embed_parts(self):
        embed = discord.Embed()
        embed.set_author(name="<@7>")
        embed.set_footer(text="<@!7>")
        message = SimpleNamespace(content="", embeds=[embed], attachments=[])
        self.assertEqual(message_text(message, identity=AgentIdentity("Synthetic Bot", 7)),
                         "@Synthetic Bot (you, member_id=7)\n"
                         "@Synthetic Bot (you, member_id=7)")

    def test_empty_embed_parts_do_not_add_empty_lines(self):
        message = SimpleNamespace(content="Synthetic text", embeds=[discord.Embed()],
                                  attachments=[])
        self.assertEqual(message_text(message), "Synthetic text")
