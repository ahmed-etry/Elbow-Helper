from dataclasses import replace
from features.agent.engine.helpers import LegacyCatalogueMixin

import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.discord_actions.messages import prepare_post
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.infrastructure.ai import AgentStep, AgentToolCall, AgentUsage
from features.agent.engine.test_agent_service import _AgentModel, _AgentSession, _context
from tests.features.agent_plan_helpers import plan_call
from . import test_agent_discord_messages as fixtures


class CurrentChannelPostTests(LegacyCatalogueMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.DiscordMessageActionTests.asyncSetUp(self)
        self.context.source_message.channel = self.channel
        self.context.delivery_channel = None
        self.target = SimpleNamespace(
            id=55, channel=self.channel, content="Synthetic question",
            jump_url="https://discord.com/channels/3/2/55",
            author=SimpleNamespace(id=7, display_name="Synthetic member", name="synthetic.7"),
        )
        self.messages[55] = self.target
        self.context.state.request_text = "Reply to <@7>."
        self.context.source_message.raw_mentions = [7]
        self.channel.permissions_for = lambda _: SimpleNamespace(
            view_channel=True, send_messages=True, read_message_history=True,
        )
        self.context.send_reply = AsyncMock(return_value={"message_ids": [200], "channel_id": 2})
        guard = patch(
            "elbow_helper.features.agent.discord_actions.messages.require_evidence_access",
            new_callable=AsyncMock,
        )
        guard.start()
        self.addCleanup(guard.stop)

    async def test_current_channel_posts_send_instead_of_preparing_a_change(self):
        for options in ({}, {"reply_to": 55}, {"reply_to": 55, "ping_author": True}):
            with self.subTest(options=options):
                with self.assertLogs(
                    "elbow_helper.features.agent.discord_actions.messages", level="INFO",
                ) as logs:
                    result = await prepare_post(self.context, {
                        "channel_id": 2, "text": "Synthetic answer", **options,
                    })
                self.assertTrue(result["delivered_as_reply"])
                self.assertEqual(result["status"], "complete")
                self.assertEqual(result["message_ids"], [200])
                self.context.send_reply.assert_awaited_with(self.context, "Synthetic answer")
                self.assertEqual(self.context.state.reply_to, options.get("reply_to"))
                self.assertEqual(self.context.state.ping_author, options.get("ping_author", False))
                self.assertEqual(self.context.state.proposed_changes, [])
                self.assertNotIn("Synthetic answer", "\n".join(logs.output))
                self.assertIn("messages=[200]", "\n".join(logs.output))

    async def test_unknown_source_reference_is_dropped_without_losing_the_answer(self):
        result = await prepare_post(self.context, {
            "channel_id": 2, "text": "My recommendation: keep them. [[message:999]]",
        })
        self.assertTrue(result["delivered_as_reply"])
        self.context.send_reply.assert_awaited_once_with(self.context, "My recommendation: keep them.")
        self.assertEqual(len(self.context.state.reply_delivery.posts), 1)

    async def test_natural_recommendation_can_be_sent_without_mandatory_quotations(self):
        text = 'I wouldn\'t push for feedback. Saying "I am not very social" isn\'t a complaint.'
        result = await prepare_post(self.context, {"channel_id": 2, "text": text})
        self.assertEqual(result["status"], "complete")
        self.context.send_reply.assert_awaited_once_with(self.context, text)

    async def test_other_channels_broadcasts_roles_and_files_still_need_confirmation(self):
        for arguments in (
            {"channel_id": 8, "text": "Synthetic answer"},
            {"channel_id": 2, "text": "@everyone Synthetic answer", "ping_everyone": True},
            {"channel_id": 2, "text": "@here Synthetic answer"},
            {"channel_id": 2, "text": "<@&9> Synthetic answer"},
            {"channel_id": 2, "text": "Synthetic answer", "file_name": "synthetic.txt"},
        ):
            with self.subTest(arguments=arguments):
                self.context.state.proposed_changes.clear()
                other = SimpleNamespace(**{**vars(self.channel), "id": arguments["channel_id"]})
                with patch(
                    "elbow_helper.features.agent.discord_actions.messages._channel_for_post",
                    new_callable=AsyncMock, return_value=other,
                ), patch(
                    "elbow_helper.features.agent.discord_actions.messages.resolve_post_attachment",
                    new_callable=AsyncMock, return_value=None,
                ):
                    result = await prepare_post(self.context, arguments)
                self.assertEqual(result["status"], "confirmation_required")
                self.assertTrue(self.context.state.proposed_changes)
                self.context.send_reply.assert_not_awaited()

    async def test_post_plan_sends_once_without_an_answer_round(self):
        context = _context()
        channel = context.source_message.channel

        async def send(local, text):
            local.state.reply_delivery.delivery.record(200, text)
            return {"message_ids": [200], "channel_id": channel.id}

        sender = AsyncMock(side_effect=send)
        context = replace(context, send_reply=sender, bot=SimpleNamespace(tree=object()))
        call = AgentToolCall("post", "post_discord_message", json.dumps({
            "channel_id": channel.id, "text": "Synthetic answer",
        }))
        session = _AgentSession([AgentStep(None, (plan_call(call),), AgentUsage())])
        with patch(
            "elbow_helper.features.agent.discord_actions.messages._channel_for_post",
            new_callable=AsyncMock, return_value=channel,
        ), patch(
            "elbow_helper.features.agent.engine.service.build_command_tools",
            return_value=({}, {}),
        ):
            response = await AgentService(_AgentModel(session)).answer(
                question="Post a synthetic answer", local_context="", context=context,
            )
        self.assertEqual(response, "")
        self.assertEqual(len(session.calls), 1)
        sender.assert_awaited_once()
        self.assertEqual(context.state.proposed_changes, [])
        self.assertEqual(context.state.reply_delivery.delivery.text_parts, ["Synthetic answer"])
