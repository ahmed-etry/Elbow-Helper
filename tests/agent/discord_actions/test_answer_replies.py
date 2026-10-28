from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.agent.delivery import AgentDeliveryMixin
from elbow_helper.agent.discord_actions.reply_targets import set_reply_target
from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.agent.models import AgentDelivery, AgentRequestContext, AgentTurnState
from . import test_agent_discord_messages as fixtures


class AnswerReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.DiscordMessageActionTests.asyncSetUp(self)
        self.context.delivery_channel = self.channel
        self.channel.permissions_for = lambda _: SimpleNamespace(
            view_channel=True, send_messages=True, read_message_history=True,
        )
        self.target = SimpleNamespace(
            id=55, channel=self.channel, content="Synthetic question",
            author=SimpleNamespace(id=7, display_name="Synthetic member"),
            reply=AsyncMock(return_value=SimpleNamespace(id=70)),
        )
        self.context.state.request_text = "Reply to <@7>."
        self.context.source_message.raw_mentions = [7]
        self.messages[55] = self.target
        self.channel.send = AsyncMock(return_value=SimpleNamespace(id=71))
        self.request = SimpleNamespace(
            id=99, channel=self.channel, archive_reply=False,
            reply=AsyncMock(return_value=SimpleNamespace(id=72)),
        )
        self.guard = patch(
            "elbow_helper.agent.discord_actions.reply_targets.require_evidence_access",
            new_callable=AsyncMock,
        )
        self.guard.start()
        self.addCleanup(self.guard.stop)
        delivery_guard = patch(
            "elbow_helper.agent.delivery.require_evidence_access", new_callable=AsyncMock,
        )
        delivery_guard.start()
        self.addCleanup(delivery_guard.stop)
        self.surface = AgentDeliveryMixin()

    async def deliver(self, chunks):
        delivery = AgentDelivery()
        await self.surface._send_response_parts(
            self.request, "\n".join(chunks), chunks, {}, None, None,
            discord.AllowedMentions.none(), delivery, delivery, self.context, None,
        )
        return delivery

    async def test_answer_replies_without_confirmation_or_default_ping(self):
        result = await set_reply_target(self.context, {"message_id": 55})
        self.assertEqual(result, {"reply_to": 55, "ping_author": False})
        self.assertEqual(self.context.state.proposed_changes, [])
        self.target.reply.assert_not_awaited()
        delivered = await self.deliver(["Synthetic answer"])
        self.target.reply.assert_awaited_once()
        self.assertFalse(self.target.reply.await_args.kwargs["mention_author"])
        self.assertFalse(self.target.reply.await_args.kwargs["allowed_mentions"].replied_user)
        self.request.reply.assert_not_awaited()
        self.assertEqual(delivered.message_ids, [70])
        self.assertTrue(delivered.complete)

    async def test_explicit_ping_is_only_on_the_first_part(self):
        await set_reply_target(self.context, {"message_id": 55, "ping_author": True})
        await self.deliver(["First part", "Second part"])
        options = self.target.reply.await_args.kwargs
        self.assertTrue(options["mention_author"])
        self.assertTrue(options["allowed_mentions"].replied_user)
        options = self.channel.send.await_args.kwargs
        self.assertFalse(options["allowed_mentions"].replied_user)

    async def test_reply_author_ping_cannot_substitute_a_different_member(self):
        self.context.state.request_text = ""
        self.context.source_message.raw_mentions = []
        self.context.state.resolved_members = {8: "ahmad (amaj)"}
        result = await set_reply_target(self.context, {"message_id": 55, "ping_author": True})
        self.assertIn("error", result)
        self.assertIsNone(self.context.state.reply_to)
        self.target.reply.assert_not_awaited()

    async def test_request_reply_author_grants_provenance_without_a_name_lookup(self):
        self.context.state.request_text = "Could he get a nudge?"
        self.context.source_message.raw_mentions = []
        self.context.source_message.channel = self.channel
        self.context.source_message.reference = SimpleNamespace(message_id=55, channel_id=2)
        self.context.state.local_messages = [{"message_id": 55, "author_id": 7}]
        result = await set_reply_target(self.context, {"message_id": 55, "ping_author": True})
        self.assertNotIn("error", result)
        await self.deliver(["Over here."])
        self.assertTrue(self.target.reply.await_args.kwargs["allowed_mentions"].replied_user)

    async def test_unreadable_and_other_channel_targets_are_refused(self):
        self.target.channel = SimpleNamespace(id=999)
        result = await set_reply_target(self.context, {"message_id": 55})
        self.assertIn("error", result)
        self.assertIsNone(self.context.state.reply_to)
        self.target.channel = self.channel
        self.channel.permissions_for = lambda _: SimpleNamespace(
            view_channel=True, send_messages=True, read_message_history=False,
        )
        result = await set_reply_target(self.context, {"message_id": 55})
        self.assertIn("error", result)
        self.assertIsNone(self.context.state.reply_to)

    async def test_delivery_rechecks_read_access_and_later_target_replaces_the_first(self):
        await set_reply_target(self.context, {"message_id": 55, "ping_author": True})
        second = SimpleNamespace(id=56, channel=self.channel, reply=AsyncMock())
        self.messages[56] = second
        await set_reply_target(self.context, {"message_id": 56})
        self.assertEqual(self.context.state.reply_to, 56)
        self.assertFalse(self.context.state.ping_author)
        self.channel.permissions_for = lambda _: SimpleNamespace(
            view_channel=True, read_message_history=False,
        )
        with self.assertRaises(ValueError):
            await self.deliver(["Synthetic answer"])
        self.target.reply.assert_not_awaited()
        second.reply.assert_not_awaited()

    async def test_backstop_delivery_keeps_only_media_and_records_the_actual_message(self):
        self.request.mentions = []
        self.request.guild = self.context.guild
        self.surface.bot = SimpleNamespace(user=None)
        context = AgentRequestContext(
            bot=self.surface.bot, guild=self.context.guild, member=self.context.member,
            source_message=self.request, account_links=None, message_search=None,
            action_repository=self.context.action_repository,
        )
        context.state.reply_to = 55
        context.state.ping_author = True
        context.state.request_text = "Reply to <@7>."
        context.source_message.raw_mentions = [7]
        url = "https://tenor.com/view/synthetic"
        context.state.gif_url = url
        with patch(
            "elbow_helper.agent.delivery.assert_answer_scope", new_callable=AsyncMock,
            return_value=True,
        ):
            result = await self.surface.send_post_reply(
                self.request, f"Synthetic answer <@7>\n{url}", None, context, None,
            )
        self.assertEqual(result, {"channel_id": 2, "message_ids": [70]})
        options = self.target.reply.await_args.kwargs
        self.assertTrue(options["mention_author"])
        self.assertFalse(options["allowed_mentions"].users)
        self.assertFalse(options["allowed_mentions"].everyone)
        self.assertFalse(options["allowed_mentions"].roles)
        self.assertEqual(self.target.reply.await_args.args[0], url)
        self.channel.send.assert_not_awaited()
        self.assertEqual(context.state.reply_delivery.delivery.text_parts,
                         [url])
        for identifier in (70,):
            recorded = self.context.action_repository.agent_message(
                message_id=identifier, guild_id=3, channel_id=2,
            )
            self.assertEqual(recorded["requester_id"], self.context.member.id)

    async def test_post_reply_quoting_a_gif_link_sends_the_full_text_once(self):
        self.request.mentions = []
        self.request.guild = self.context.guild
        self.surface.bot = SimpleNamespace(user=None)
        context = AgentRequestContext(
            bot=self.surface.bot, guild=self.context.guild, member=self.context.member,
            source_message=self.request, account_links=None, message_search=None,
            action_repository=self.context.action_repository,
        )
        text = "Synthetic first message.\nSynthetic second message.\nhttps://tenor.com/view/synthetic"
        with patch(
            "elbow_helper.agent.delivery.assert_answer_scope", new_callable=AsyncMock,
            return_value=True,
        ):
            result = await self.surface.send_post_reply(self.request, text, None, context, None)
        self.assertEqual(result, {"channel_id": 2, "message_ids": [72]})
        self.request.reply.assert_awaited_once()
        self.assertEqual(self.request.reply.await_args.args[0], text)
        self.channel.send.assert_not_awaited()
        self.assertEqual(context.state.reply_delivery.delivery.text_parts, [text])

    def test_registered_reply_selection_is_output_and_transient(self):
        from elbow_helper.agent.actions.contracts import ActionClass
        tool = build_agent_tools()["set_discord_reply_target"]
        self.assertEqual(tool.action_class, ActionClass.OUTPUT)
        self.assertIsNone(AgentTurnState().reply_to)


    async def test_same_channel_post_mentions_uncached_members_without_reply_ping(self):
        self.request.mentions = []
        self.request.guild = self.context.guild
        self.surface.bot = SimpleNamespace(user=None)
        context = AgentRequestContext(
            bot=self.surface.bot, guild=self.context.guild, member=self.context.member,
            source_message=self.request, account_links=None, message_search=None,
        )
        context.state.reply_to = 55
        context.state.request_text = "Post to <@12345> and <@54321>."
        context.source_message.raw_mentions = [12345, 54321]
        with patch("elbow_helper.agent.delivery.assert_answer_scope", new_callable=AsyncMock,
                   return_value=True):
            await self.surface.send_post_reply(
                self.request, "Synthetic answer <@12345> <@!54321>", None, context, None,
            )
        options = self.target.reply.await_args.kwargs
        self.assertFalse(options["mention_author"])
        self.assertEqual(options["allowed_mentions"].to_dict(), {"parse": ["users"], "roles": []})
