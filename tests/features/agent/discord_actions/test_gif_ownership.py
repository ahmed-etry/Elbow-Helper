"""Synthetic plans assign a found GIF to references or message content, never two deliveries."""

import unittest

from elbow_helper.features.agent.discord_actions.gif_ownership import consume_gifs
from elbow_helper.features.agent.models import AgentTurnState


class GifOwnershipTests(unittest.TestCase):
    def run_plan(self, capability, arguments, *, result=None):
        state = AgentTurnState(gif_url="https://tenor.com/view/synthetic")
        steps = {"gif": {"capability": "find_gif", "arguments": {}},
                 "consumer": {"capability": capability, "arguments": arguments}}
        results = {"gif": {"url": state.gif_url}, "consumer": result or {"value": "synthetic"}}
        consume_gifs(state, steps, results)
        return state, steps, results

    def test_reference_consumes_the_gif(self):
        state, _, _ = self.run_plan("synthetic_consumer", {
            "value": {"step": "gif", "path": ["url"]},
        })
        self.assertIsNone(state.gif_url)

    def test_message_post_edit_and_dm_claim_literal_links(self):
        for capability in ("post_discord_message", "edit_agent_message", "send_direct_messages"):
            state, _, _ = self.run_plan(capability, {
                "text": "Synthetic text https://tenor.com/view/synthetic",
            })
            self.assertIsNone(state.gif_url)

    def test_unused_and_failed_consumers_do_not_claim_the_gif(self):
        url = "https://tenor.com/view/synthetic"
        cases = (("synthetic_read", url, None),
                 ("post_discord_message", "Unrelated synthetic text", None),
                 ("post_discord_message", url, {"error": "Synthetic refusal"}))
        for capability, text, result in cases:
            state, _, _ = self.run_plan(capability, {"text": text}, result=result)
            self.assertIsNotNone(state.gif_url)

    def test_revisions_keep_consumption_and_allow_a_different_unused_gif(self):
        state, steps, results = self.run_plan("post_discord_message", {
            "text": {"step": "gif", "path": ["url"]},
        })
        state.gif_url = "https://tenor.com/view/synthetic"
        consume_gifs(state, steps, results)
        self.assertIsNone(state.gif_url)
        state.gif_url = "https://tenor.com/view/different-synthetic"
        consume_gifs(state, steps, results)
        self.assertEqual(state.gif_url, "https://tenor.com/view/different-synthetic")
