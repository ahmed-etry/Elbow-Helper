"""Optional identity checks cannot turn ordinary chat into an asker lookup."""

from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.engine.social_reply import social_reply_definition
from elbow_helper.agent.research.member_identity import clarification
from elbow_helper.agent.research.members import find_discord_members
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
from agent.engine.helpers import social_step
from agent.engine.test_agent_service import _AgentModel, _AgentSession
from agent.engine.test_banter_gifs import gif_context
from agent.research.test_member_identity import context, member


LOOKUP = "elbow_helper.agent.engine.social_members.find_discord_members"


class SocialMemberTests(unittest.IsolatedAsyncioTestCase):
    async def test_asker_names_ids_mentions_and_unnamed_chat_never_run_discovery(self):
        for question, query, own in (
            ("lol", None, False), ("W", "Ahmad", False), ("gm gang", "Ahmad", False),
            ("Roast Ahmad", "Ahmad", False), ("Roast owner", "owner", False),
            ("Roast <@42>", "<@42>", False), ("Roast 42", "42", False),
            ("I am Ahmad amaj", "Ahmad amaj", True),
            ("Roast him", "him", False), ("lol", "Ahmad amaj", False),
        ):
            with self.subTest(question=question, query=query):
                target = context()
                links = Mock()
                target = replace(target, account_links=links)
                target.guild.chunk = AsyncMock()
                extra = {"self_target": own, **({"member_query": query} if query else {})}
                session = _AgentSession([social_step("Synthetic acknowledgement.", **extra)])
                with patch(LOOKUP, AsyncMock(wraps=find_discord_members)) as lookup:
                    answer = await AgentService(_AgentModel(session, route="social")).answer(
                        question=question, local_context="", context=target,
                    )
                self.assertEqual(answer, "Synthetic acknowledgement.")
                self.assertEqual(len(session.calls), 1)
                lookup.assert_not_awaited()
                links.get_links_for_members.assert_not_called()
                target.guild.chunk.assert_not_awaited()
                self.assertEqual(target.state.resolved_members, {})
        definition = social_reply_definition({}).parameters
        self.assertNotIn("member_query", definition["required"])

    async def test_qualified_third_party_gets_own_identity_before_rewriting(self):
        target = context()
        target.source_message.add_reaction = AsyncMock()

        target = replace(gif_context(target), bot=SimpleNamespace())
        session = _AgentSession([
            social_step("You built me.", member_query="ahmad amaj", form="text_reactions",
                        reactions=["\U0001f44d"]),
            social_step("Ahmad amaj wears the imaginary paper crown.", member_query="ahmad amaj",
                        meme_id="synthetic reaction"),
            AgentStep("Ahmad amaj wears the imaginary paper crown.", (), AgentUsage()),
        ])
        with patch(LOOKUP, AsyncMock(wraps=find_discord_members)) as lookup:
            answer = await AgentService(_AgentModel(session, route="social")).answer(
                question="Roast ahmad amaj too", local_context="Synthetic owner is a different Ahmad.",
                context=target,
            )
        self.assertEqual(answer, "Ahmad amaj wears the imaginary paper crown.")
        lookup.assert_awaited_once()
        self.assertEqual(target.state.resolved_members, {101: "ahmad amaj"})
        result = json.loads(session.calls[1][0][0].content)
        self.assertEqual(result["member"]["member_id"], 101)
        self.assertEqual(result["member"]["username"], "amaj")
        self.assertIn("not facts about this one", result["instruction"])
        self.assertIn("creator identity", result["instruction"])
        target.source_message.add_reaction.assert_not_awaited()

    async def test_third_party_ambiguity_returns_safe_clarification_before_any_media(self):
        target = context()
        others = [member(720001, "alex.one", nickname="Alex"),
                  member(720002, "alex.two", nickname="Alex"),
                  member(720003, "alex.three", nickname="Alex Third")]
        target.guild.members.extend(others)
        target.guild.get_member = {item.id: item for item in target.guild.members}.get
        target.source_message.add_reaction = AsyncMock()

        target = replace(gif_context(target), bot=SimpleNamespace())
        session = _AgentSession([social_step("Synthetic roast.", member_query="Alex",
            form="text_reactions", reactions=["\U0001f44d"])])
        answer = await AgentService(_AgentModel(session, route="social")).answer(
            question="Roast Alex", local_context="", context=target,
        )
        self.assertIn("Alex (@alex.one)", answer)
        self.assertIn("Alex (@alex.two)", answer)
        self.assertIn("Alex Third", answer)
        self.assertNotIn("alex.three", answer)
        for other in others:
            self.assertNotIn(str(other.id), answer)
        self.assertNotIn("ID", answer)
        self.assertEqual(len(session.calls), 1)
        target.source_message.add_reaction.assert_not_awaited()

    async def test_identity_rewrite_cannot_switch_to_another_member(self):
        target = context()
        session = _AgentSession([
            social_step("Synthetic initial line.", member_query="ahmad amaj"),
            social_step("Synthetic changed target.", member_query="owner"),
            AgentStep("Which member did you mean?", (), AgentUsage()),
        ])
        with patch(LOOKUP, AsyncMock(wraps=find_discord_members)) as lookup:
            answer = await AgentService(_AgentModel(session, route="social")).answer(
                question="Roast ahmad amaj, not owner", local_context="", context=target,
            )
        self.assertEqual(answer, "Which member did you mean?")
        self.assertFalse(session.calls[-1][1])
        self.assertIn("Keep the resolved member target", session.instructions[-1])
        lookup.assert_awaited_once()
        self.assertEqual(target.state.resolved_members, {101: "ahmad amaj"})

    def test_public_clarification_suppresses_ids_in_queries_and_unique_name_usernames(self):
        people = [member(720001, "alex.one", nickname="Alex"),
                  member(720002, "blair.two", nickname="Blair")]
        for query in ("<@720001>", "720001", "Alex"):
            answer = clarification(query, people)
            self.assertIn("Alex", answer)
            self.assertIn("Blair", answer)
            self.assertNotIn("@", answer)
            self.assertNotIn("720001", answer)
            self.assertNotIn("720002", answer)
        self.assertNotIn("720001", clarification("<@720001>", []))
