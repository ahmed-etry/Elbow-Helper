import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from elbow_helper.features.agent.discord_actions.reactions import react_to_request, server_reaction_context
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
from features.agent.engine.test_agent_plan_flow import _context, _Model, _Session


def _server_emoji(name, identifier, *, usable=True, animated=False):
    item = Mock()
    item.name = name
    item.id = identifier
    item.is_usable.return_value = usable
    item.__str__ = Mock(return_value=f"<{'a' if animated else ''}:{name}:{identifier}>")
    return item


class ReactionTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_request_prompt_lists_only_usable_server_emoji_with_exact_markup(self):
        context = _context()
        available = _server_emoji("syntheticNod", 101)
        animated = _server_emoji("syntheticSalute", 102, animated=True)
        unavailable = _server_emoji("syntheticLocked", 103, usable=False)
        context.guild.emojis = (available, animated, unavailable)
        session = _Session([AgentStep("Synthetic reply", (), AgentUsage())], [])
        model = _Model(session)
        with patch("elbow_helper.features.agent.engine.service.build_agent_tools", return_value={}):
            await AgentService(model).answer(
                question="Synthetic banter", local_context="Synthetic context", context=context,
            )
        self.assertIn("Synthetic context", model.request["prompt"])
        self.assertIn("Available server reaction emojis: <:syntheticNod:101> <a:syntheticSalute:102>",
                      model.request["prompt"])
        self.assertNotIn("syntheticLocked", model.request["prompt"])
        self.assertEqual(server_reaction_context(SimpleNamespace()), "")
        self.assertEqual(server_reaction_context(SimpleNamespace(emojis=(unavailable,))), "")

    async def test_server_reactions_resolve_cached_static_and_animated_emoji(self):
        for animated in (False, True):
            with self.subTest(animated=animated):
                item = _server_emoji("syntheticNod", 101, animated=animated)
                context = _context()
                context.source_message.add_reaction = AsyncMock()
                context.guild.get_emoji = Mock(return_value=item)
                result = await react_to_request(context, {"emoji": str(item)})
                self.assertEqual(result, {"reacted": True})
                context.guild.get_emoji.assert_called_once_with(101)
                context.source_message.add_reaction.assert_awaited_once_with(item)
                self.assertEqual(context.state.reactions.emojis, [str(item)])

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
            "elbow_helper.features.agent.discord_actions.reactions.require_evidence_access",
            AsyncMock(),
        ):
            results = await asyncio.gather(*(
                react_to_request(context, {"emoji": "👍"}) for _ in range(4)
            ))
        self.assertEqual(message.add_reaction.await_count, 3)
        self.assertEqual(sum("error" in result for result in results), 1)
        context.source_message = SimpleNamespace(id=0)
        self.assertEqual((await react_to_request(context, {"emoji": "👍"}))["cause"], "missing_message")
