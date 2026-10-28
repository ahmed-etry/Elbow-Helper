"""Synthetic forum posts validate tags and retain replies when creation undo is requested."""

import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.agent.discord_actions.forum_posts import prepare_post, undo_post
from agent.discord_actions.management_helpers import Entity, fixture
from agent.discord_actions.test_channel_management import history


class ForumPostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        access = patch("elbow_helper.agent.discord_actions.management."
                       "require_evidence_access", new_callable=AsyncMock)
        access.start()
        self.addCleanup(access.stop)

    def context(self):
        context, channel, _, member = fixture()
        channel.type = discord.ChannelType.forum
        channel.flags = Entity(require_tag=True)
        channel.available_tags = [Entity(id=5, name="Synthetic tag", moderated=False)]
        thread = Entity(id=900, parent_id=channel.id, owner_id=context.guild.me.id,
                        mention="#synthetic-post", guild=context.guild, delete=AsyncMock(),
                        history=lambda **kwargs: history([]))
        original = context.guild.get_channel_or_thread
        context.guild.get_channel_or_thread = lambda identifier: (
            thread if identifier == thread.id else original(identifier)
        )
        channel.create_thread = AsyncMock(return_value=Entity(
            thread=thread, message=Entity(id=900),
        ))
        return context, channel, thread, member

    async def test_confirmed_post_keeps_exact_content_and_undo_refuses_member_replies(self):
        context, channel, thread, member = self.context()
        arguments = {"channel_id": 200, "title": "Synthetic title",
                     "content": "Synthetic content <@40>", "tag_ids": [5]}
        await prepare_post(context, arguments)
        channel.create_thread.assert_not_called()
        action = context.state.proposed_changes[0]
        result = await action.run()
        self.assertEqual(channel.create_thread.await_args.kwargs["content"], arguments["content"])
        self.assertFalse(channel.create_thread.await_args.kwargs["allowed_mentions"].everyone)
        undo = await undo_post(context, {"targets": action.values, "after": result.after})
        member.bot = False
        thread.history = lambda **kwargs: history([Entity(id=901, author=member)])
        with self.assertRaisesRegex(ValueError, "member has replied"):
            await undo.run()
        thread.delete.assert_not_called()

    async def test_missing_and_moderated_tags_are_checked_before_preview(self):
        context, channel, _, _ = self.context()
        arguments = {"channel_id": 200, "title": "Synthetic", "content": "Synthetic",
                     "tag_ids": [6]}
        with self.assertRaisesRegex(ValueError, "tags are unavailable"):
            await prepare_post(context, arguments)
        channel.available_tags[0].moderated = True
        context.guild.me.guild_permissions = discord.Permissions(
            view_channel=True, send_messages=True,
        )
        arguments["tag_ids"] = [5]
        with self.assertRaisesRegex(ValueError, "The bot needs Manage Threads"):
            await prepare_post(context, arguments)
        channel.create_thread.assert_not_called()
