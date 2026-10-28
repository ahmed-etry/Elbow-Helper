"""Channel repetition checks use successful bot metadata only."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.agent.discord_actions.reactions import channel_reaction_history, react_to_request
from elbow_helper.agent.models import ChannelReactions
from agent.discord_actions.test_agent_reactions import _server_emoji
from agent.engine.test_agent_service import _context


class ChannelReactionTests(unittest.IsolatedAsyncioTestCase):
    def context(self, channel=None, identifier=300):
        context = _context()
        context.state.channel_reactions = channel if channel is not None else ChannelReactions()
        context.source_message.id = identifier
        context.source_message.add_reaction = AsyncMock()
        return context

    def test_history_ignores_other_users_reactions_and_never_reads_member_content(self):
        class MetadataMessage:
            def __init__(self, identifier, reactions):
                self.id = identifier
                self.reactions = reactions

            @property
            def content(self):
                raise AssertionError("Member-written content must not be read")

        def reaction(value, me):
            return SimpleNamespace(emoji=value, me=me)

        rows = [MetadataMessage(1, [reaction("\U0001f914", True)]),
                MetadataMessage(2, [reaction("\U0001f680", True), reaction("\U0001f389", True)]),
                MetadataMessage(3, [reaction("\U0001f44d", False)])]
        self.assertEqual(channel_reaction_history(rows), (2, ("\U0001f680", "\U0001f389")))
        self.assertEqual(channel_reaction_history(rows[-1:]), (0, ()))

    async def test_previous_reaction_spans_members_and_conversations_but_channels_are_independent(self):
        channel = ChannelReactions()
        first, second, third = (self.context(channel, index) for index in (300, 301, 302))
        await react_to_request(first, {"emoji": "\U0001f914"})
        result = await react_to_request(second, {"emoji": "\U0001f914"})
        self.assertEqual(result["cause"], "repeated_reaction")
        second.source_message.add_reaction.assert_not_awaited()
        await react_to_request(second, {"emoji": "\U0001f680"})
        self.assertTrue((await react_to_request(third, {"emoji": "\U0001f914"}))["reacted"])
        self.assertTrue((await react_to_request(self.context(), {"emoji": "\U0001f914"}))["reacted"])

    async def test_custom_emoji_aliases_renames_and_unicode_selectors_cannot_bypass_repetition(self):
        item = _server_emoji("syntheticNewName", 101)
        for selection in ("syntheticNewName", ":syntheticNewName:", "<:syntheticNewName:101>"):
            context = self.context(ChannelReactions(emojis=("<:syntheticOldName:101>",)))
            context.guild.emojis = (item,)
            context.guild.get_emoji = lambda identifier: item
            self.assertEqual((await react_to_request(context, {"emoji": selection}))["cause"], "repeated_reaction")
            context.source_message.add_reaction.assert_not_awaited()
        context = self.context(ChannelReactions(emojis=("\u2764\ufe0f",)))
        self.assertEqual((await react_to_request(context, {"emoji": "\u2764"}))["cause"], "repeated_reaction")

    async def test_failed_send_keeps_previous_reaction_and_concurrent_requests_do_not_repeat(self):
        channel = ChannelReactions(emojis=("\U0001f914",))
        failed = self.context(channel)
        failed.source_message.add_reaction.side_effect = OSError("Synthetic delivery failure")
        await react_to_request(failed, {"emoji": "\U0001f680"})
        self.assertEqual(channel.emojis, ("\U0001f914",))
        contexts = [self.context(channel, index) for index in (301, 302)]
        results = await asyncio.gather(*(react_to_request(context, {"emoji": "\U0001f680"}) for context in contexts))
        self.assertEqual(sum(result["reacted"] for result in results), 1)
        self.assertEqual(sum(context.source_message.add_reaction.await_count for context in contexts), 1)

    async def test_unknown_history_skips_reactions_and_keeps_the_line(self):
        from elbow_helper.agent.engine.service import AgentService
        from agent.engine.helpers import social_step
        from agent.engine.test_agent_service import _AgentModel, _AgentSession

        context = self.context(ChannelReactions(emojis=None))
        session = _AgentSession([social_step("Synthetic line.", form="text_reactions", reactions=["\U0001f914"])])
        with patch("elbow_helper.agent.discord_actions.reactions.require_evidence_access", AsyncMock()):
            answer = await AgentService(_AgentModel(session, route="social")).answer(
                question="Synthetic exchange", local_context="", context=context,
            )
        self.assertEqual(answer, "Synthetic line.")
        context.source_message.add_reaction.assert_not_awaited()

    async def test_cancelled_combo_remembers_reactions_already_sent(self):
        context = self.context()
        context.state.reaction_combo_due = True
        context.source_message.add_reaction.side_effect = [None, asyncio.CancelledError()]
        with self.assertRaises(asyncio.CancelledError):
            await react_to_request(context, {"emoji": ["\U0001f914", "\U0001f680"]})
        self.assertEqual(context.state.channel_reactions.emojis, ("\U0001f914",))
