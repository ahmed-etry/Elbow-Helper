"""Attack plans use the member's images and preview all posted pages."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.commands.adapters.attack_plans import (
    attack_plan_adapters, prepare_attack_plan,
)
from elbow_helper.features.attack_plans.cog import Planning


class AttackPlanCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepared_plan_posts_the_previewed_images_and_pages(self):
        workflow = object.__new__(Planning)
        workflow.clash_client = SimpleNamespace(configured=True)
        workflow.clan_health = SimpleNamespace(search_players=lambda *args: [
            {"player_name": "Planner", "player_tag": "#P0Y"},
        ])
        emoji_set = SimpleNamespace(tokens={}, get=lambda _: None)
        workflow.plan_emojis = SimpleNamespace(
            get=AsyncMock(return_value=emoji_set),
        )
        strategy = SimpleNamespace(
            id=10, url="https://example.com/strategy.png",
            content_type="image/png",
        )
        base = SimpleNamespace(
            id=11, url="https://example.com/base.png",
            content_type="image/png",
        )
        channel = SimpleNamespace(
            id=9, mention="<#9>", send=AsyncMock(return_value=SimpleNamespace(id=99)),
            permissions_for=lambda _: SimpleNamespace(
                view_channel=True, send_messages=True,
            ),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=SimpleNamespace(me=SimpleNamespace(id=3)),
            member=SimpleNamespace(id=4),
            source_message=SimpleNamespace(
                channel=channel, attachments=[strategy, base],
            ),
        )
        values = {"player": "Planner", "thinking": "Enter from the right.",
                  "strategy_image": 10, "base_image": 11}
        player = {"_http_status": 200, "name": "Planner",
                  "townHallLevel": 18, "heroes": [], "pets": [],
                  "heroEquipment": [], "troops": [], "spells": []}
        with patch("elbow_helper.features.attack_plans.cog.fetch_player",
                   new=AsyncMock(return_value=player)) as fetch:
            prepared = await prepare_attack_plan(context, values)
        self.assertTrue(await prepared.preview.recheck())
        self.assertIn("Army screenshot: https://example.com/strategy.png",
                      prepared.preview.lines)
        self.assertIn("Base screenshot: https://example.com/base.png",
                      prepared.preview.lines)
        self.assertTrue(any("Page 3:" in line for line in prepared.preview.lines))
        self.assertIs(attack_plan_adapters()[0].classification,
                      ActionClass.CHANGE)
        channel.send.assert_not_awaited()
        result = await prepared.run()
        self.assertEqual(result.result["message_id"], 99)
        self.assertEqual(channel.send.await_count, 1)
        self.assertEqual(channel.send.await_args.kwargs["embeds"][0].image.url,
                         strategy.url)
        fetch.assert_awaited_once()

    async def test_missing_message_attachment_is_rejected(self):
        workflow = object.__new__(Planning)
        channel = SimpleNamespace(
            id=9, permissions_for=lambda _: SimpleNamespace(
                view_channel=True, send_messages=True,
            ),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=SimpleNamespace(me=SimpleNamespace(id=3)),
            member=SimpleNamespace(id=4),
            source_message=SimpleNamespace(channel=channel, attachments=[]),
        )
        with self.assertRaisesRegex(ValueError, "Attach both screenshots"):
            await prepare_attack_plan(context, {
                "player": "#P0Y", "thinking": "Approach",
                "strategy_image": 10, "base_image": 11,
            })
