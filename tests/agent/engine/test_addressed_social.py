"""Directed social replies retain their verified recipient through composition."""

import json
import unittest
from types import SimpleNamespace

from elbow_helper.agent.conversation.state import ConversationTurn
from tests.agent.engine.helpers import social_step
from tests.agent.request_fixtures import member_context as _context
from tests.agent.loop_stub import banter_answer, final


def recipient():
    return SimpleNamespace(id=701, name="sample.user", display_name="Sample Member",
                           global_name="Sample Member", nick="Sample Member", aliases=(),
                           mention="<@701>", roles=[], bot=False)


class AddressedSocialTests(unittest.IsolatedAsyncioTestCase):
    async def test_directed_reply_mentions_recipient_after_identity_rewrite(self):
        for text in ("Synthetic line.", "<@701> Synthetic line.", "<@!701> Synthetic line."):
            with self.subTest(text=text):
                context = _context()
                person = recipient()
                context.guild.members = [context.member, person]
                context.guild.get_member = {item.id: item for item in context.guild.members}.get
                answer, _, _ = await banter_answer(
                    context,
                    social_step("Draft.", member_query="Sample Member", address_member=True),
                    social_step(text, member_query="Sample Member"),
                    question="Say hello to Sample Member")
                expected = text if text.startswith("<@") else "<@701> " + text
                self.assertEqual(answer, expected)
                self.assertEqual(context.state.resolved_members, {701: "Sample Member"})

    async def test_follow_up_addresses_a_member_the_asker_named_in_an_earlier_turn(self):
        # "they're the same guy" names nobody; the asker named Sample Member in the turn before.
        for earlier, expected in (
            ("Roast Sample Member", "<@701> Synthetic line."),
            ("Roast him", "Which member?"),
        ):
            with self.subTest(earlier=earlier):
                context = _context()
                context.guild.members = [context.member, recipient()]
                context.guild.get_member = {item.id: item for item in context.guild.members}.get
                context.state.authorized_history = (ConversationTurn(
                    text=json.dumps({"question": earlier, "answer": "Which one?",
                                     "member_id": str(context.member.id)}),
                    source_channels=frozenset()),)
                answer, _, _ = await banter_answer(
                    context,
                    social_step("Draft.", member_query="Sample Member", address_member=True),
                    (social_step("Synthetic line.", member_query="Sample Member")
                     if earlier == "Roast Sample Member" else final("Which member?")),
                    question="They're the same person")
                self.assertEqual(answer, expected)

    async def test_unnamed_tease_addresses_the_author_of_the_replied_message(self):
        # "nuke him" sent as a reply to Sample Member's message names nobody in the request.
        for query, expected, reply_to in (("Sample Member", "Synthetic line.", 555),
                                          ("Other Person", "Which member?", None)):
            with self.subTest(query=query):
                context = _context()
                person, other = recipient(), SimpleNamespace(**{**vars(recipient()), "id": 702,
                    "name": "other.user", "display_name": "Other Person", "global_name": "Other Person",
                    "nick": "Other Person", "mention": "<@702>"})
                context.guild.members = [context.member, person, other]
                context.guild.get_member = {item.id: item for item in context.guild.members}.get
                channel = context.source_message.channel
                context.source_message.reference = SimpleNamespace(
                    channel_id=channel.id, message_id=555,
                    resolved=SimpleNamespace(id=555, channel=channel, guild=context.guild, author=person))
                steps = [social_step("Draft.", member_query=query, address_member=True, meme_id="trump_wrong")]
                if reply_to:
                    steps.append(social_step("Synthetic line.", member_query=query))
                else:
                    steps.append(final("Which member?"))
                answer, session, _ = await banter_answer(context, *steps, question="nuke him")
                self.assertEqual(answer, expected)
                # The line, or a GIF sent instead, replies to the member it is aimed at.
                self.assertEqual(context.state.reply_to, reply_to)
                self.assertIsNone(context.state.gif_url)
                if reply_to is None:
                    self.assertIn("Member identity could not be resolved", session.calls[-1][0].content)

    async def test_tease_in_reply_pings_once_by_the_reply_and_names_mentions_only_when_no_message(self):
        # "nuke him" sent as a reply to Sample Member's message: the reply pings them, the text does not.
        context = _context()
        person = recipient()
        context.guild.members = [context.member, person]
        context.guild.get_member = {item.id: item for item in context.guild.members}.get
        channel = context.source_message.channel
        context.source_message.reference = SimpleNamespace(
            channel_id=channel.id, message_id=555,
            resolved=SimpleNamespace(id=555, channel=channel, guild=context.guild, author=person))
        answer, session, _ = await banter_answer(
            context,
            social_step("Draft.", member_query="Sample Member", address_member=True),
            social_step("<@701> Synthetic line.", member_query="Sample Member"),
            question="nuke him")
        self.assertEqual(answer, "Synthetic line.")
        self.assertEqual((context.state.reply_to, context.state.ping_author), (555, True))
        shown = "".join(result.content for result in session.calls[-1])
        self.assertIn("This reply goes to their message, which pings them", shown)
        self.assertNotIn("use their Discord mention", shown)

        named = _context()
        named.guild.members = [named.member, recipient()]
        named.guild.get_member = {item.id: item for item in named.guild.members}.get
        answer, session, _ = await banter_answer(
            named,
            social_step("Draft.", member_query="Sample Member", address_member=True),
            social_step("Synthetic line.", member_query="Sample Member"),
            question="Say hello to Sample Member")
        self.assertEqual((answer, named.state.ping_author), ("<@701> Synthetic line.", False))
        self.assertIn("use their Discord mention", "".join(result.content for result in session.calls[-1]))

    async def test_reply_about_member_does_not_add_a_mention(self):
        context = _context()
        person = recipient()
        context.guild.members = [context.member, person]
        context.guild.get_member = {item.id: item for item in context.guild.members}.get
        answer, _, _ = await banter_answer(
            context,
            social_step("Draft.", member_query="Sample Member", address_member=False),
            social_step("Synthetic line.", member_query="Sample Member"),
            question="Sample Member is great")
        self.assertEqual(answer, "Synthetic line.")
