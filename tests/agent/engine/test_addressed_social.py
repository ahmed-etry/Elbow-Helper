"""Directed social replies retain their verified recipient through composition."""

import unittest
from types import SimpleNamespace

from elbow_helper.agent.engine.service import AgentService
from tests.agent.engine.helpers import social_step
from tests.agent.engine.test_agent_service import _AgentModel, _AgentSession, _context


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
                session = _AgentSession([
                    social_step("Draft.", member_query="Sample Member", address_member=True),
                    social_step(text, member_query="Sample Member"),
                ])
                answer = await AgentService(_AgentModel(session, route="social")).answer(
                    question="Say hello to Sample Member", local_context="", context=context,
                )
                expected = text if text.startswith("<@") else "<@701> " + text
                self.assertEqual(answer, expected)
                self.assertEqual(context.state.resolved_members, {701: "Sample Member"})

    async def test_unnamed_tease_addresses_the_author_of_the_replied_message(self):
        # "nuke him" sent as a reply to Sample Member's message names nobody in the request.
        for query, expected, reply_to in (("Sample Member", "Synthetic line.", 555),
                                          ("Other Person", "Draft.", None)):
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
                session = _AgentSession([
                    social_step("Draft.", member_query=query, address_member=True),
                    social_step("Synthetic line.", member_query=query),
                ])
                answer = await AgentService(_AgentModel(session, route="social")).answer(
                    question="nuke him", local_context="", context=context,
                )
                self.assertEqual(answer, expected)
                # The line, or a GIF sent instead, replies to the member it is aimed at.
                self.assertEqual(context.state.reply_to, reply_to)

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
        session = _AgentSession([
            social_step("Draft.", member_query="Sample Member", address_member=True),
            social_step("<@701> Synthetic line.", member_query="Sample Member"),
        ])
        answer = await AgentService(_AgentModel(session, route="social")).answer(
            question="nuke him", local_context="", context=context)
        self.assertEqual(answer, "Synthetic line.")
        self.assertEqual((context.state.reply_to, context.state.ping_author), (555, True))
        shown = session.instructions[-1] + "".join(result.content for result in session.calls[-1][0])
        self.assertIn("This reply goes to their message, which pings them", shown)
        self.assertNotIn("use their Discord mention", shown)

        named = _context()
        named.guild.members = [named.member, recipient()]
        named.guild.get_member = {item.id: item for item in named.guild.members}.get
        session = _AgentSession([
            social_step("Draft.", member_query="Sample Member", address_member=True),
            social_step("Synthetic line.", member_query="Sample Member"),
        ])
        answer = await AgentService(_AgentModel(session, route="social")).answer(
            question="Say hello to Sample Member", local_context="", context=named)
        self.assertEqual((answer, named.state.ping_author), ("<@701> Synthetic line.", False))
        self.assertIn("use their Discord mention", session.instructions[-1])

    async def test_reply_about_member_does_not_add_a_mention(self):
        context = _context()
        person = recipient()
        context.guild.members = [context.member, person]
        context.guild.get_member = {item.id: item for item in context.guild.members}.get
        session = _AgentSession([
            social_step("Draft.", member_query="Sample Member", address_member=False),
            social_step("Synthetic line.", member_query="Sample Member"),
        ])
        answer = await AgentService(_AgentModel(session, route="social")).answer(
            question="Sample Member is great", local_context="", context=context,
        )
        self.assertEqual(answer, "Synthetic line.")
