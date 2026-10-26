"""GIF tooling exposes only the application meme library."""

import unittest
from unittest.mock import patch

from elbow_helper.features.agent.discord_actions.gif_library import ITEM_BY_ID
from elbow_helper.features.agent.discord_actions.gifs import find_gif
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.plan.format import system_instructions
from features.agent.engine.test_agent_service import _context


class GifTests(unittest.IsolatedAsyncioTestCase):
    def test_registration_schema_and_prompt_use_the_library(self):
        registry = build_agent_tools()
        tool = registry["find_gif"].definition
        self.assertIn("images are sent without text", tool.description)
        self.assertIn("visual reaction beats text", tool.description)
        self.assertIn("never paste GIF links", tool.usage_note)
        self.assertIn("Social replies name a library meme in submit_social_reply", tool.usage_note)
        self.assertEqual(tool.parameters["properties"]["query"]["enum"], list(ITEM_BY_ID))
        self.assertIn("A selected image is the whole reply", system_instructions(registry, social=True))

    async def test_empty_library_rejects_unknown_item(self):
        context = _context()
        with patch.dict(ITEM_BY_ID, {}, clear=True):
            self.assertEqual(await find_gif(context, {"query": "empty"}),
                             {"error": "No fitting GIF in the library."})
        self.assertIsNone(context.state.gif_url)
