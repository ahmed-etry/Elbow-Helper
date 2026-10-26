"""Reply evidence follows explicit targets rather than neighbouring applicants."""

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.plan.checker import check_plan
from elbow_helper.features.agent.plan.executor import execute_plan
from elbow_helper.features.agent.research.history import read_message_context


def message(identifier, content, *, author=10, reply_to=None, channel_id=100):
    return SimpleNamespace(
        id=identifier, content=content, author=SimpleNamespace(id=author, display_name=f"Member {author}"),
        created_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
        embeds=[], attachments=[], channel=SimpleNamespace(id=channel_id),
        reference=SimpleNamespace(message_id=reply_to, channel_id=channel_id,
                                  type=discord.MessageReferenceType.default) if reply_to else None,
    )


class ReplyTargetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.quote = message(300, "leave it for a week", reply_to=150)
        self.neighbour = message(299, "New applicant is ready", author=20)
        self.target = message(150, "Should the old advertisement stay up?", author=30)
        self.rows = [self.neighbour, self.quote]
        async def history(**kwargs):
            for row in self.rows:
                yield row
        self.channel = SimpleNamespace(id=100, name="rec-room", history=history,
                                       fetch_message=AsyncMock(return_value=self.target))
        self.context = SimpleNamespace(guild=SimpleNamespace(id=1),
                                       state=SimpleNamespace(source_channels=set()))

    async def read(self, **arguments):
        with patch("elbow_helper.features.agent.research.history.accessible_message_channel",
                   return_value=self.channel):
            return await read_message_context(self.context, {"channel_id": 100, "message_id": 300, **arguments})

    async def test_parent_outside_neighbours_is_fetched_with_its_own_author_and_source(self):
        result = await self.read()
        self.channel.fetch_message.assert_awaited_once_with(150)
        self.assertTrue(result["requested_message_found"])
        self.assertEqual(result["reply_target"]["content"], self.target.content)
        self.assertEqual(result["reply_target"]["author_id"], 30)
        self.assertEqual(result["reply_target"]["source"], "https://discord.com/channels/1/100/150")
        self.assertEqual(result["messages"][1]["reply_to_message_id"], 150)
        self.assertEqual(result["reply_target"]["status"], "read")

    async def test_parent_already_in_context_needs_no_extra_read(self):
        self.rows.insert(0, self.target)
        result = await self.read()
        self.channel.fetch_message.assert_not_awaited()
        self.assertEqual(result["reply_target"]["message_id"], 150)

    async def test_omitted_reference_channel_uses_the_message_channel(self):
        self.quote.reference.channel_id = None
        result = await self.read()
        self.assertEqual(result["reply_target"]["message_id"], 150)
        self.channel.fetch_message.assert_awaited_once_with(150)

    async def test_deleted_parent_is_unavailable_not_replaced_by_neighbour(self):
        self.channel.fetch_message.side_effect = discord.NotFound(SimpleNamespace(status=404, reason="Missing"), "gone")
        result = await self.read()
        self.assertEqual(result["reply_target"], {"message_id": 150, "status": "unavailable"})

    async def test_no_reference_does_not_turn_adjacency_into_a_reply(self):
        self.quote.reference = None
        result = await self.read()
        self.assertIsNone(result["reply_target"])
        self.channel.fetch_message.assert_not_awaited()

    async def test_cross_channel_and_forward_references_are_not_followed(self):
        for field, value in (("channel_id", 200), ("type", discord.MessageReferenceType.forward)):
            self.quote.reference = SimpleNamespace(
                message_id=150, channel_id=100, type=discord.MessageReferenceType.default,
            )
            setattr(self.quote.reference, field, value)
            result = await self.read()
            self.assertIsNone(result["reply_target"])
            self.channel.fetch_message.assert_not_awaited()

    async def test_out_of_period_parent_is_not_fetched(self):
        lower = discord.utils.time_snowflake(datetime(2026, 10, 20, tzinfo=timezone.utc))
        self.quote.id = lower + 10
        self.quote.reference.message_id = lower - 10
        with patch("elbow_helper.features.agent.research.history.accessible_message_channel", return_value=self.channel):
            result = await read_message_context(self.context, {
                "channel_id": 100, "message_id": self.quote.id,
                "after": "2026-10-20", "before": "2026-10-22",
            })
        self.assertEqual(result["reply_target"]["status"], "outside_requested_period")
        self.channel.fetch_message.assert_not_awaited()

    async def test_wrong_channel_parent_is_discarded(self):
        self.target.channel.id = 200
        result = await self.read()
        self.assertEqual(result["reply_target"]["status"], "unavailable")
        self.assertNotIn("content", result["reply_target"])

    async def test_access_loss_after_parent_read_discards_all_messages(self):
        with patch("elbow_helper.features.agent.research.history.accessible_message_channel",
                   side_effect=[self.channel, None]):
            result = await read_message_context(self.context, {"channel_id": 100, "message_id": 300})
        self.assertIn("error", result)
        self.assertNotIn("messages", result)
        self.assertNotIn("reply_target", result)
        self.assertFalse(self.context.state.source_channels)

    async def test_parent_context_can_be_planned_before_the_quote_is_read(self):
        registry = build_agent_tools()
        plan = {
            "goal": "Resolve the subject of a synthetic quote", "effort": "high", "output": "text",
            "steps": [
                {"id": "quote", "capability": "read_message_context", "depends_on": [],
                 "arguments": {"channel_id": 100, "message_id": 300}},
                {"id": "parent", "capability": "read_message_context", "depends_on": [],
                 "arguments": {"channel_id": 100,
                               "message_id": {"step": "quote", "path": ["reply_target", "message_id"]}}},
            ],
        }
        self.assertTrue(check_plan(plan, registry).ok)
        async def history(**kwargs):
            for row in ([self.target] if kwargs["around"].id == 150 else self.rows):
                yield row
        self.channel.history = history
        async def run(step, arguments, earlier):
            return await registry[step["capability"]].handler(self.context, arguments)
        with patch("elbow_helper.features.agent.research.history.accessible_message_channel", return_value=self.channel):
            results = await execute_plan(plan, run, argument_schema=lambda step:
                                         registry[step["capability"]].definition.parameters)
        self.assertTrue(results["parent"]["requested_message_found"])
        self.assertEqual(results["parent"]["messages"][0]["content"], self.target.content)


if __name__ == "__main__":
    unittest.main()
