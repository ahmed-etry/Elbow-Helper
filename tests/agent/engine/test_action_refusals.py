"""Refused changes explain their result before another confirmation is offered."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import discord

from elbow_helper.agent.actions.contracts import ActionClass
from elbow_helper.agent.engine.tool_call import execute_tool
from tests.agent.request_fixtures import flow_context as _context


class ActionRefusalTests(unittest.IsolatedAsyncioTestCase):

    async def test_unexpected_value_error_is_logged_and_masked(self):
        async def fail(context, arguments):
            raise ValueError("Synthetic internal diagnostic")
        for action_class in ActionClass:
            with self.subTest(action_class=action_class):
                with patch("elbow_helper.agent.engine.tool_call.LOGGER.exception") as diagnostic:
                    result = await execute_tool(
                        name="synthetic_tool", handler=fail, arguments={}, context=_context(),
                        action_class=action_class,
                    )
                self.assertEqual(json.loads(result), {"error": "That lookup failed because of a bot error."
                    if action_class is ActionClass.READ else "I couldn't run that command."})
                diagnostic.assert_called_once()

    async def test_discord_failures_are_named_by_kind_and_logged(self):
        context = _context()
        limited = "Discord limited the bot's requests. Try again shortly."
        for label, error, message in (
            ('forbidden', discord.Forbidden(SimpleNamespace(status=403, reason='Synthetic'), 'Private diagnostic'),
             'Discord refused the request (status 403).'),
            ('not found', discord.NotFound(SimpleNamespace(status=404, reason='Synthetic'), 'Private diagnostic'),
             "Discord couldn't find it (status 404)."),
            ('429', discord.HTTPException(SimpleNamespace(status=429, reason='Synthetic'), 'Private diagnostic'),
             limited),
            ('rate limited', discord.RateLimited(2.0), limited),
            ('503', discord.HTTPException(SimpleNamespace(status=503, reason='Synthetic'), 'Private diagnostic'),
             "Discord didn't answer the request (status 503)."),
            ('connection closed', discord.ConnectionClosed(SimpleNamespace(close_code=1006), shard_id=None),
             "Discord didn't answer the request."),
        ):
            async def fail(context, arguments):
                context.state.source_channels.add(123)
                raise error
            for action_class in ActionClass:
                with self.subTest(label=label, action_class=action_class):
                    with patch('elbow_helper.agent.engine.tool_call.LOGGER.exception') as diagnostic:
                        result = await execute_tool(
                            name='synthetic_tool', handler=fail, arguments={}, context=context,
                            action_class=action_class)
                    self.assertEqual(json.loads(result), {'error': message, 'cause': 'source_unavailable'})
                    self.assertFalse(context.state.source_channels)
                    diagnostic.assert_called_once()

    async def test_connection_errors_are_discords_only_when_discord_raised_them(self):
        import aiohttp
        namespace = {'__name__': 'discord.synthetic', 'aiohttp': aiohttp}
        exec("def drop():\n    raise aiohttp.ClientConnectionError('Synthetic')", namespace)

        async def from_discord(context, arguments):
            namespace['drop']()

        async def elsewhere(context, arguments):
            raise aiohttp.ClientConnectionError('Synthetic')

        for handler, message in ((from_discord, "Discord didn't answer the request."),
                                 (elsewhere, 'That lookup failed because of a bot error.')):
            result = await execute_tool(
                name='synthetic_tool', handler=handler, arguments={}, context=_context())
            self.assertEqual(json.loads(result)['error'], message)
