"""Message actions record their own posts and refuse unrelated messages."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.actions.repository import AgentActionRepository
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.tools.discord_messages import (
    prepare_delete, prepare_edit, prepare_post,
)


class DiscordMessageActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        repository = AgentActionRepository(Path(self.directory.name) / "actions.sqlite3")
        bot_member = SimpleNamespace(id=1)
        messages = {}
        channel = SimpleNamespace(id=2, mention="#place")
        channel.guild = SimpleNamespace(id=3)
        channel.permissions_for = lambda _: SimpleNamespace(view_channel=True, send_messages=True)
        async def send(text, **kwargs):
            message_id = 100 + len(messages)
            message = SimpleNamespace(id=message_id, content=text, author=bot_member,
                                      nonce=kwargs["nonce"])
            async def edit(*, content, allowed_mentions):
                message.content = content
                return message
            async def delete():
                messages.pop(message_id)
            message.edit = AsyncMock(side_effect=edit)
            message.delete = AsyncMock(side_effect=delete)
            messages[message_id] = message
            return message
        async def fetch(message_id):
            return messages[message_id]
        channel.send = AsyncMock(side_effect=send)
        channel.fetch_message = AsyncMock(side_effect=fetch)
        guild = SimpleNamespace(
            id=3, me=bot_member, get_channel_or_thread=lambda _: channel,
            get_role=lambda _: None,
        )
        self.context = SimpleNamespace(
            guild=guild, bot=SimpleNamespace(), member=SimpleNamespace(id=4),
            state=AgentTurnState(), action_repository=repository,
        )
        self.channel = channel
        self.messages = messages
        self.repository = repository

    async def test_long_post_previews_each_part_and_records_only_its_messages(self):
        with patch("elbow_helper.features.agent.tools.discord_messages.require_evidence_access",
                   new_callable=AsyncMock):
            result = await prepare_post(self.context, {
                "channel_id": 2, "text": "word " * 500,
            })
        self.assertEqual(result["prepared_count"], 2)
        for action in self.context.state.command_proposals:
            self.assertTrue(await action.preview.recheck())
            outcome = await action.run()
            self.assertTrue(await action.verify())
            self.assertIsNotNone(self.repository.agent_message(
                message_id=outcome.after["message_id"], guild_id=3, channel_id=2,
            ))
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_edit_and_delete_require_a_recorded_agent_post(self):
        with patch("elbow_helper.features.agent.tools.discord_messages.require_evidence_access",
                   new_callable=AsyncMock):
            await prepare_post(self.context, {"channel_id": 2, "text": "First"})
            post = self.context.state.command_proposals.pop()
            posted = await post.run()
            message_id = posted.after["message_id"]
            edit = await prepare_edit(self.context, {
                "channel_id": 2, "message_id": message_id, "text": "Second",
            })
            self.assertEqual(edit["status"], "confirmation_required")
            change = self.context.state.command_proposals.pop()
            self.assertTrue(await change.preview.recheck())
            await change.run()
            self.assertEqual(self.messages[message_id].content, "Second")
            removal = await prepare_delete(self.context, {
                "channel_id": 2, "message_id": message_id,
            })
            self.assertEqual(removal["status"], "confirmation_required")
            deletion = self.context.state.command_proposals.pop()
            self.assertIs(deletion.action_class, ActionClass.IRREVERSIBLE)
            await deletion.run()
            self.assertIsNone(self.repository.agent_message(
                message_id=message_id, guild_id=3, channel_id=2,
            ))
            refused = await prepare_edit(self.context, {
                "channel_id": 2, "message_id": message_id, "text": "Third",
            })
            self.assertIn("error", refused)
