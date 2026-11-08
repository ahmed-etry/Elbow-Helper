"""Discord presentation and cancellation ordering at the final send boundary."""

import asyncio
from types import SimpleNamespace
import unittest

from elbow_helper.agent.activity import reply_typing
from elbow_helper.agent.text import discord_text


class ReplyPresentationTests(unittest.IsolatedAsyncioTestCase):
    def test_tables_become_bullets_without_losing_columns(self):
        text = "Candidates:\n\n| Member | TH | Heroes |\n|---|---:|---|\n| A | 17 | 100/100 |\n| B | 18 | 110/110 |\n\nDone."
        self.assertEqual(discord_text(text), "Candidates:\n\n- Member: A; TH: 17; Heroes: 100/100\n- Member: B; TH: 18; Heroes: 110/110\n\nDone.")

    def test_code_literal_pipes_and_other_text_are_preserved(self):
        code = "```md\n| A | B |\n|---|---|\n| x | y |\n```\n"
        self.assertEqual(discord_text(code), code)
        text = "Name | Value\n--- | ---\nA\\|B | `x|y`"
        self.assertEqual(discord_text(text), "- Name: A\\|B; Value: `x|y`")
        for text in ("a | b", "Text\n\nwith **formatting**\n", "|a|b|\nnot a separator\n|c|d|"):
            self.assertEqual(discord_text(text), text)

    async def test_typing_cancellation_is_joined(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        class Activity:
            async def __aenter__(self):
                async def refresh():
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        await asyncio.sleep(0)
                        cancelled.set()
                self.task = asyncio.create_task(refresh())
                await entered.wait()

            async def __aexit__(self, *args):
                self.task.cancel()

        activity = Activity()
        async with reply_typing(SimpleNamespace(typing=lambda: activity)):
            self.assertFalse(cancelled.is_set())
        self.assertTrue(cancelled.is_set())
        self.assertTrue(activity.task.done())
