"""GIF search uses a configured application client and returns page links."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from elbow_helper.infrastructure.gifs import GiphyClient
from elbow_helper.core.settings import RuntimeSettings
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.discord_actions.gifs import find_gif
from elbow_helper.features.agent.plan.format import system_instructions


class GifTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_parameters_result_and_close(self):
        client = GiphyClient("synthetic-key")
        response = SimpleNamespace(
            status=200,
            json=AsyncMock(return_value={"data": [{"url": "https://giphy.com/gifs/example"}]}),
        )
        request = MagicMock()
        request.__aenter__ = AsyncMock(return_value=response)
        request.__aexit__ = AsyncMock(return_value=None)
        session = SimpleNamespace(
            closed=False, get=MagicMock(return_value=request), close=AsyncMock(),
        )
        client._session = session
        async with client:
            self.assertEqual(await client.search("war win"), "https://giphy.com/gifs/example")
            self.assertEqual(session.get.call_args.kwargs["params"], {
                "api_key":"synthetic-key", "q":"war win", "limit":1, "rating":"pg-13", "lang":"en"})
            response.json.return_value = {"data":[]}
            self.assertIsNone(await client.search("empty"))
            response.status = 429
            self.assertIsNone(await client.search("later"))
        session.close.assert_awaited_once()
        self.assertIsNone(client._session)

    async def test_conditional_registration_prompt_and_no_result(self):
        disabled = build_agent_tools(GiphyClient(None))
        enabled = build_agent_tools(GiphyClient("synthetic"))
        self.assertNotIn("find_gif", disabled)
        self.assertIn("find_gif", enabled)
        self.assertNotIn("When a GIF fits", system_instructions(disabled))
        self.assertIn("When a GIF fits", system_instructions(enabled))
        context = SimpleNamespace(bot=SimpleNamespace(
            gif_client=SimpleNamespace(search=AsyncMock(return_value=None)),
        ))
        self.assertEqual(await find_gif(context, {"query":"empty"}), {"error":"No GIF found."})
        self.assertEqual(
            RuntimeSettings.from_mapping({"GIPHY_API_KEY": "  synthetic  "}).giphy_api_key,
            "synthetic",
        )
        self.assertIsNone(RuntimeSettings.from_mapping({}).giphy_api_key)
