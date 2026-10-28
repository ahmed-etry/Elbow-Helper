"""Synthetic message moves preserve content and attachments and link unfinished messages."""

from io import BytesIO
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.agent.discord_actions.message_moves import prepare_move
from agent.discord_actions.management_helpers import Entity, fixture
from agent.discord_actions.test_message_cleanup import add_messages


class MessageMoveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        access = patch("elbow_helper.agent.discord_actions.management."
                       "require_evidence_access", new_callable=AsyncMock)
        access.start()
        self.addCleanup(access.stop)

    def context(self):
        context, source, _, member = fixture()
        messages = add_messages(source, member, ages=(1, 2))
        target = Entity(id=201, guild=context.guild, mention="#synthetic-target",
                        send=AsyncMock(return_value=Entity(id=901)),
                        permissions_for=source.permissions_for)
        original = context.guild.get_channel_or_thread
        context.guild.get_channel_or_thread = lambda identifier: (
            target if identifier == target.id else original(identifier)
        )
        arguments = {"source_channel_id": 200, "target_channel_id": 201, "message_ids": [1, 2]}
        return context, source, target, messages, arguments

    async def test_exact_messages_and_files_are_reposted_without_mentions_then_deleted(self):
        context, source, target, messages, arguments = self.context()
        attachment = Entity(id=11, filename="synthetic.txt", size=9,
                            to_file=AsyncMock(return_value=discord.File(
                                BytesIO(b"synthetic"), filename="synthetic.txt",
                            )))
        messages[1].attachments = [attachment]
        messages[1].content = "Synthetic <@40> @everyone"
        contents, files = [], []

        async def send(content, **options):
            contents.append(content)
            files.extend(file.fp.read() for file in options["files"])
            self.assertFalse(options["allowed_mentions"].everyone)
            self.assertFalse(options["allowed_mentions"].users)
            source.delete_messages.assert_not_called()
            return Entity(id=901)

        target.send.side_effect = send
        await prepare_move(context, arguments)
        target.send.assert_not_called()
        action = context.state.proposed_changes[0]
        self.assertEqual(action.action_class.value, "irreversible")
        result = await action.run()
        self.assertIn("<t:", contents[0])
        self.assertTrue(contents[0].endswith(messages[1].content))
        self.assertEqual(files, [b"synthetic"])
        self.assertEqual(result.result["moved_ids"], [1, 2])
        self.assertEqual(result.text, "")
        self.assertEqual(source.history.call_count, 2)
        source.fetch_message.assert_not_awaited()
        self.assertEqual(source.delete_messages.await_args.args[0], list(messages.values()))

    async def test_a_failed_repost_preserves_that_original_and_reports_partial_results(self):
        context, source, target, messages, arguments = self.context()
        target.send.side_effect = [Entity(id=901), discord.Forbidden(
            Entity(status=403, reason="Synthetic"), {"code": 50013, "message": "Synthetic"},
        )]
        await prepare_move(context, arguments)
        result = await context.state.proposed_changes[0].run()
        self.assertEqual(result.result["moved_ids"], [1])
        self.assertEqual("Not moved: " + messages[2].jump_url, result.text)
        self.assertEqual(source.delete_messages.await_args.args[0], [messages[1]])
        self.assertEqual(target.send.await_count, 2)

    async def test_failed_original_deletion_is_reported_as_reposted_but_not_deleted(self):
        context, source, _, _, arguments = self.context()
        source.delete_messages.side_effect = discord.Forbidden(
            Entity(status=403, reason="Synthetic"), {"code": 50013, "message": "Synthetic"},
        )
        await prepare_move(context, arguments)
        result = await context.state.proposed_changes[0].run()
        self.assertEqual(result.result["moved_ids"], [])
        links = ", ".join(f"https://discord.com/channels/1/200/{identifier}" for identifier in (1, 2))
        self.assertEqual(result.text, f"Not moved: {links}\nReposted but not deleted: {links}")

    async def test_source_target_permissions_and_same_channel_refuse_before_preview(self):
        for failure in ("source", "target", "same"):
            context, _, target, _, arguments = self.context()
            if failure == "source":
                context.member.guild_permissions = discord.Permissions(
                    view_channel=True, read_message_history=True, send_messages=True,
                )
            elif failure == "target":
                target.permissions_for = lambda actor: discord.Permissions(view_channel=True)
            else:
                arguments["target_channel_id"] = 200
            with self.assertRaises(ValueError):
                await prepare_move(context, arguments)
            target.send.assert_not_called()
            self.assertFalse(context.state.proposed_changes)

    async def test_long_messages_preserve_every_character_across_reposts(self):
        context, _, target, messages, arguments = self.context()
        arguments["message_ids"] = [1]
        messages[1].content = "  Synthetic content\n" * 180
        await prepare_move(context, arguments)
        await context.state.proposed_changes[0].run()
        parts = [call.args[0] for call in target.send.await_args_list]
        self.assertTrue(all(len(part) <= 2000 for part in parts))
        self.assertGreater(len(parts), 1)
        self.assertTrue("".join(parts).endswith(messages[1].content))

    async def test_attachment_size_is_checked_before_any_repost(self):
        context, _, target, messages, arguments = self.context()
        messages[1].attachments = [Entity(id=11, filename="synthetic-large.txt",
                                           size=context.guild.filesize_limit + 1)]
        with self.assertRaisesRegex(ValueError, "attachments exceed"):
            await prepare_move(context, arguments)
        target.send.assert_not_called()
