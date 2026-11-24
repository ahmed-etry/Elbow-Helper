"""Offline social-output checks using the real library and reply plan."""

import unittest
from unittest.mock import patch

from elbow_helper.agent.discord_actions.gif_library import ITEM_BY_ID
from elbow_helper.agent.discord_actions.gifs import find_gif
from tests.agent.request_fixtures import member_context as _context
from tests.agent.engine.helpers import social_step
from tests.agent.loop_stub import banter_answer


def gif_context(context=None, *, recent=()):
    """Supply an authorized channel snapshot independently of retained turns."""
    context = _context() if context is None else context
    context.state.channel_bot_gifs = tuple(recent)
    return context


class BanterGifTests(unittest.IsolatedAsyncioTestCase):
    async def test_recent_gif_does_not_block_library_selection(self):
        context = gif_context(recent=(True,))
        url = "https://tenor.com/view/synthetic-new"
        with patch.dict(ITEM_BY_ID, synthetic={"id": "synthetic", "media": url}):
            result = await find_gif(context, {"query": "synthetic"})
        self.assertEqual(result, {"url": url})

    async def test_unreadable_library_file_keeps_the_planned_fallback(self):
        with (patch.dict(ITEM_BY_ID, synthetic={
            "id": "synthetic", "media": "missing.gif", "use": "Synthetic reaction",
            "shows": "Synthetic image", "group": "win",
        }), patch("elbow_helper.agent.discord_actions.gif_library._read_media", return_value=None)):
            context = gif_context()
            answer, session, _ = await banter_answer(
                context, social_step("Synthetic fallback.", meme_id="synthetic"),
                question="The imaginary tower fell over.")
        self.assertEqual(answer, "Synthetic fallback.")
        self.assertIsNone(context.state.gif_url)
        self.assertEqual(len(session.calls), 1)
