"""Raffle prize changes show prior values and restore them on undo."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.capabilities.achievements.commands import (
    prepare_raffle_prize, prepare_raffle_prize_undo, run_raffle_prize,
)


class RafflePrizeCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_apply_and_undo_use_public_feature_operations(self):
        state = [24117, "Old prize", "2"]

        async def apply(prize, winners):
            state[1:] = [prize, str(winners)]
            return f"Saved this month's raffle prize for {winners} winners: {prize}"

        async def restore(month, prize, winners):
            self.assertEqual(month, state[0])
            state[1:] = [prize, winners]

        workflow = SimpleNamespace(
            raffle_prize_state=AsyncMock(side_effect=lambda: tuple(state)),
            apply_raffle_prize=AsyncMock(side_effect=apply),
            restore_raffle_prize=AsyncMock(side_effect=restore),
        )
        context = SimpleNamespace(bot=SimpleNamespace(get_cog=lambda _: workflow))
        values = {"prize": "New prize", "winners": 3}
        preview = await prepare_raffle_prize(context, values)
        self.assertIn("Prize: Old prize to New prize", preview.lines)
        self.assertTrue(await preview.recheck())
        workflow.apply_raffle_prize.assert_not_awaited()
        outcome = await run_raffle_prize(context, values)
        self.assertEqual(outcome.visibility, "private")
        undo = await prepare_raffle_prize_undo(context, {
            "before": preview.before, "after": outcome.after,
        })
        self.assertTrue(await undo.preview.recheck())
        await undo.run()
        self.assertEqual(state[1:], ["Old prize", "2"])
        workflow.restore_raffle_prize.assert_awaited_once()
