"""GIF tooling exposes only the application meme library."""

import unittest
from unittest.mock import patch

from elbow_helper.agent.discord_actions.gif_library import ITEM_BY_ID
from elbow_helper.agent.discord_actions.gifs import find_gif
from tests.agent.engine.test_agent_service import _context


class GifTests(unittest.IsolatedAsyncioTestCase):

    async def test_empty_library_rejects_unknown_item(self):
        context = _context()
        with patch.dict(ITEM_BY_ID, {}, clear=True):
            self.assertEqual(await find_gif(context, {"query": "empty"}),
                             {"error": "No fitting GIF in the library."})
        self.assertIsNone(context.state.gif_url)
