"""Directed social replies retain their verified recipient through composition."""

import unittest
from types import SimpleNamespace

from elbow_helper.features.agent.engine.service import AgentService
from features.agent.engine.helpers import social_step
from features.agent.engine.test_agent_service import _AgentModel, _AgentSession, _context


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
