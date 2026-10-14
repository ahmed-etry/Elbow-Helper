"""Exact synthetic messages are reviewed, age-separated and never purged by search."""

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

from elbow_helper.features.agent.discord_actions.message_cleanup import prepare_cleanup, selected_messages
from elbow_helper.features.agent.actions.contracts import ActionClass
from features.agent.discord_actions.management_helpers import Entity, fixture


def add_messages(channel, member, ages=(1, 2, 15)):
    messages = {index: Entity(
        id=index, author=member, content=f"Synthetic message {index}", pinned=index == 2,
        created_at=datetime.now(timezone.utc) - timedelta(days=age),
        jump_url=f"https://discord.com/channels/1/200/{index}", attachments=[], delete=AsyncMock(),
    ) for index, age in enumerate(ages, 1)}
    channel.fetch_message = AsyncMock(side_effect=messages.__getitem__)
    async def history(*, limit, after, before, oldest_first):
        for identifier in sorted(messages, reverse=not oldest_first)[:limit]:
            if after.id < identifier < before.id:
                yield messages[identifier]
    channel.history = Mock(side_effect=history)
    channel.delete_messages = AsyncMock()
    return messages


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        access = patch("elbow_helper.features.agent.discord_actions.management."
                       "require_evidence_access", new_callable=AsyncMock)
        access.start()
        self.addCleanup(access.stop)

    async def test_exact_selection_is_previewed_then_bulk_and_single_deleted(self):
        context, channel, _, member = fixture()
        messages = add_messages(channel, member)
        await prepare_cleanup(context, {"channel_id": 200, "message_ids": [1, 2, 3]})
        channel.delete_messages.assert_not_called()
        action = context.state.proposed_changes[0]
        self.assertIs(action.action_class, ActionClass.IRREVERSIBLE)
        self.assertIn("(pinned)", "\n".join(action.preview.details))
        self.assertIn("<t:", "\n".join(action.preview.details))
        result = await action.run()
        self.assertEqual(channel.delete_messages.await_args.args[0], [messages[1], messages[2]])
        messages[3].delete.assert_awaited_once()
        self.assertEqual(result.result["deleted_ids"], [1, 2, 3])
        self.assertEqual(result.text, "")
        self.assertEqual(channel.history.call_count, 2)
        channel.fetch_message.assert_not_awaited()
        self.assertTrue(action.preview.details[0].startswith(
            "[Synthetic member](https://discord.com/channels/1/200/1), <t:"))

    async def test_own_messages_need_no_requester_manage_messages(self):
        context, channel, _, _ = fixture()
        context.member.guild_permissions = discord.Permissions(view_channel=True,
                                                               read_message_history=True)
        messages = add_messages(channel, context.member, ages=(1,))
        await prepare_cleanup(context, {"channel_id": 200, "message_ids": [1]})
        messages[1].author = Entity(id=777)
        with self.assertRaises(ValueError):
            await context.state.proposed_changes[0].run()
        channel.delete_messages.assert_not_called()

    async def test_partial_failure_links_the_undeleted_messages_without_retrying(self):
        context, channel, _, member = fixture()
        messages = add_messages(channel, member)
        messages[3].delete.side_effect = discord.Forbidden(
            Entity(status=403, reason="Synthetic"), {"code": 50013, "message": "Synthetic"},
        )
        await prepare_cleanup(context, {"channel_id": 200, "message_ids": [1, 3]})
        result = await context.state.proposed_changes[0].run()
        self.assertEqual("Not deleted: " + messages[3].jump_url, result.text)
        messages[3].delete.assert_awaited_once()

    async def test_changed_message_blocks_deletion_after_preview_recheck(self):
        context, channel, _, member = fixture()
        messages = add_messages(channel, member)
        await prepare_cleanup(context, {"channel_id": 200, "message_ids": [1, 2]})
        action = context.state.proposed_changes[0]
        self.assertTrue(await action.preview.recheck())
        messages[1].content = "Changed synthetic message"
        with self.assertRaisesRegex(ValueError, "changed"):
            await action.run()
        channel.delete_messages.assert_not_awaited()

    async def test_sparse_selection_fetches_only_ids_outside_the_history_page(self):
        context, channel, _, member = fixture()
        messages = add_messages(channel, member, ages=(1,) * 201)
        _, selected, missing = await selected_messages(context, 200, [1, 102, 201])
        self.assertEqual(selected, [messages[1], messages[102], messages[201]])
        self.assertEqual(missing, [])
        channel.fetch_message.assert_awaited_once_with(1)
        self.assertEqual(channel.history.call_count, 1)

    async def test_missing_message_inside_page_needs_no_single_fetch(self):
        context, channel, _, member = fixture()
        messages = add_messages(channel, member)
        del messages[2]
        _, selected, missing = await selected_messages(context, 200, [1, 2, 3], allow_missing=True)
        self.assertEqual([message.id for message in selected], [1, 3])
        self.assertEqual(missing, [2])
        channel.fetch_message.assert_not_awaited()

    async def test_caps_and_bot_permissions_are_checked_before_preview(self):
        for ids in ([1, 1], list(range(1, 102)), []):
            context, channel, _, member = fixture()
            add_messages(channel, member)
            with self.assertRaises(ValueError):
                await prepare_cleanup(context, {"channel_id": 200, "message_ids": ids})
            self.assertFalse(context.state.proposed_changes)
        context, channel, _, member = fixture()
        add_messages(channel, member)
        context.guild.me.guild_permissions = discord.Permissions(view_channel=True,
                                                                 read_message_history=True)
        with self.assertRaisesRegex(ValueError, "The bot needs Manage Messages"):
            await prepare_cleanup(context, {"channel_id": 200, "message_ids": [1]})
