import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

import aiohttp

from elbow_helper.agent.discord_actions.reactions import react_to_request
from elbow_helper.agent.models import AgentTurnState
from agent.engine.test_agent_plan_flow import _context


def _server_emoji(name, identifier, *, usable=True, animated=False):
    item = Mock()
    item.name = name
    item.id = identifier
    item.is_usable.return_value = usable
    item.__str__ = Mock(return_value=f"<{'a' if animated else ''}:{name}:{identifier}>")
    return item


class ReactionTests(unittest.IsolatedAsyncioTestCase):

    async def test_combo_applies_cached_and_unicode_reactions_in_order(self):
        thinking = _server_emoji("syntheticThink", 101)
        launching = _server_emoji("syntheticLaunch", 102, animated=True)
        context = _context()
        context.state.reaction_combo_due = True
        context.guild.emojis = (thinking, launching)
        context.guild.get_emoji = Mock(return_value=launching)
        context.source_message.add_reaction = AsyncMock()
        result = await react_to_request(context, {"emoji": ["syntheticThink", "\U0001f4a1", str(launching)]})
        self.assertEqual(context.source_message.add_reaction.await_args_list,
                         [call(thinking), call("\U0001f4a1"), call(launching)])
        self.assertEqual(context.state.reactions.emojis, [str(thinking), "\U0001f4a1", str(launching)])
        self.assertEqual(context.state.reactions.count, 3)
        self.assertTrue(result["reacted"])
        self.assertEqual(result["flags"]["status"], "complete")
        self.assertEqual(result["results"], [{"reacted": True}] * 3)

    async def test_failed_middle_reaction_keeps_successes_and_attempts_the_last(self):
        import discord
        for exception, cause in (
            (discord.Forbidden(SimpleNamespace(status=403, reason="synthetic"), "synthetic"), "permission_denied"),
            (discord.HTTPException(SimpleNamespace(status=500, reason="synthetic"), "synthetic"), "delivery_failed"),
            (aiohttp.ClientConnectionError("Synthetic disconnect"), "delivery_failed"),
            (OSError("Synthetic network failure"), "delivery_failed"),
            (TimeoutError(), "delivery_failed"),
        ):
            with self.subTest(cause=cause):
                context = _context()
                context.state.reaction_combo_due = True
                context.source_message.add_reaction = AsyncMock(side_effect=[None, exception, None])
                result = await react_to_request(context, {"emoji": ["\U0001f914", "\U0001f4a1", "\U0001f680"]})
                self.assertEqual(context.source_message.add_reaction.await_args_list,
                                 [call("\U0001f914"), call("\U0001f4a1"), call("\U0001f680")])
                self.assertEqual(context.state.reactions.emojis, ["\U0001f914", "\U0001f680"])
                self.assertEqual(context.state.reactions.count, 2)
                self.assertTrue(result["reacted"])
                self.assertEqual(result["flags"]["status"], "partial")
                self.assertEqual(result["results"][1]["cause"], cause)


    async def test_combos_do_not_interleave_and_share_the_request_cap(self):
        context = _context()
        context.state.reaction_combo_due = True
        sent = []

        async def add(value):
            await asyncio.sleep(0)
            sent.append(value)

        context.source_message.add_reaction = AsyncMock(side_effect=add)
        results = await asyncio.gather(
            react_to_request(context, {"emoji": ["\U0001f914", "\U0001f4a1"]}),
            react_to_request(context, {"emoji": ["\U0001f680", "\U0001f389"]}),
        )
        self.assertEqual(sent, ["\U0001f914", "\U0001f4a1"])
        self.assertEqual(context.state.reactions.emojis, sent)
        self.assertEqual(context.state.reactions.count, 2)
        self.assertEqual(results[0]["flags"]["status"], "complete")
        self.assertEqual(results[1]["cause"], "combo_not_offered")

    async def test_handler_rejects_empty_or_oversized_combos_before_delivery(self):
        for selections in ([], ["\U0001f914"] * 4):
            with self.subTest(selections=selections):
                context = _context()
                context.source_message.add_reaction = AsyncMock()
                result = await react_to_request(context, {"emoji": selections})
                self.assertEqual(result["cause"], "invalid_selection")
                context.source_message.add_reaction.assert_not_awaited()

    async def test_cached_names_shortcodes_and_markup_resolve_to_the_same_usable_emoji(self):
        item = _server_emoji("syntheticNod", 101)
        for value in ("syntheticNod", ":syntheticNod:", ":SYNTHETICNOD", "<:syntheticNod:101>"):
            with self.subTest(value=value):
                context = _context()
                context.guild.emojis = (item,)
                context.guild.get_emoji = Mock(return_value=item)
                context.source_message.add_reaction = AsyncMock()
                self.assertEqual(await react_to_request(context, {"emoji": value}), {"reacted": True})
                context.source_message.add_reaction.assert_awaited_once_with(item)

    async def test_failure_causes_distinguish_selection_limit_permissions_and_delivery(self):
        import discord
        context = _context()
        context.state.reaction_combo_due = True
        context.source_message.add_reaction = AsyncMock()
        self.assertEqual((await react_to_request(context, {"emoji": "missing_selection"}))["cause"], "invalid_selection")
        context.state.reactions.count = 3
        self.assertEqual((await react_to_request(context, {"emoji": "👍"}))["cause"], "reaction_limit")
        context.state.reactions.count = 0
        for exception, cause in ((discord.Forbidden(SimpleNamespace(status=403, reason="denied"), "denied"), "permission_denied"),
                                 (discord.HTTPException(SimpleNamespace(status=500, reason="failed"), "failed"), "delivery_failed")):
            context.source_message.add_reaction.side_effect = exception
            self.assertEqual((await react_to_request(context, {"emoji": "👍"}))["cause"], cause)
        self.assertEqual(context.state.reactions.count, 0)


    async def test_unknown_or_unusable_custom_emoji_and_invalid_input_do_not_react(self):
        for item in (None, _server_emoji("syntheticLocked", 103, usable=False)):
            for value in ("<:syntheticLocked:103>", "<a:syntheticLocked:103>", ":unknown:", "text"):
                with self.subTest(item=item, value=value):
                    context = _context()
                    context.source_message.add_reaction = AsyncMock()
                    context.guild.get_emoji = Mock(return_value=item)
                    result = await react_to_request(context, {"emoji": value})
                    self.assertIn("error", result)
                    context.source_message.add_reaction.assert_not_awaited()
                    self.assertFalse(context.state.reactions.emojis)

    async def test_reaction_cap_is_shared_across_parallel_steps(self):
        message = SimpleNamespace(id=1, add_reaction=AsyncMock())
        context = SimpleNamespace(source_message=message, state=AgentTurnState())
        with patch(
            "elbow_helper.agent.discord_actions.reactions.require_evidence_access",
            AsyncMock(),
        ):
            results = await asyncio.gather(*(
                react_to_request(context, {"emoji": "👍"}) for _ in range(4)
            ))
        self.assertEqual(message.add_reaction.await_count, 1)
        self.assertEqual(sum("error" in result for result in results), 3)
        context.source_message = SimpleNamespace(id=0)
        self.assertEqual((await react_to_request(context, {"emoji": "👍"}))["cause"], "missing_message")
