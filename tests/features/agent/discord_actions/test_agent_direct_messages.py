"""DM confirmation, access, pacing and delivery outcomes."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.features.agent.discord_actions.direct_messages import (
    prepare_direct_messages,
    deliver_dms,
    dm_recipients,
)
from elbow_helper.features.agent.actions.contracts import ActionRefused
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.actions.report import format_run_report


class DirectMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_refusals_return_specific_messages_and_access_to_the_model(self):
        from elbow_helper.features.agent.engine.tool_call import execute_tool

        self.context.source_message = SimpleNamespace(id=1)
        cases = (
            ({"member_ids": [99], "text": "Synthetic"},
             "Choose 1 to 50 distinct current server members."),
            ({"member_ids": [1], "text": ""},
             "The DM text must be 1 to 4,000 characters."),
            ({"member_ids": [1], "text": "x" * 4001},
             "The DM text must be 1 to 4,000 characters."),
        )
        with (
            patch(
                "elbow_helper.features.agent.engine.tool_call.require_evidence_access", AsyncMock(),
            ),
            patch(
                "elbow_helper.features.agent.discord_actions.direct_messages."
                "require_evidence_access",
                AsyncMock(),
            ),
            patch(
                "elbow_helper.features.agent.discord_actions.direct_messages."
                "has_access_requirements",
                return_value=False,
            ),
        ):
            for arguments, expected in cases:
                result = await execute_tool(
                    name="send_direct_messages", handler=prepare_direct_messages,
                    context=self.context, arguments=arguments,
                )
                self.assertEqual(json.loads(result)["error"], expected)
            result = await execute_tool(
                name="send_direct_messages", handler=prepare_direct_messages,
                context=self.context, arguments={"member_ids": [2], "text": "Synthetic"},
            )
            self.assertEqual(json.loads(result), {
                "error": "DMs to other members need lead access.",
                "required_access": ["lead"],
            })
            self.members[2].bot = True
            result = await execute_tool(
                name="send_direct_messages", handler=prepare_direct_messages,
                context=self.context, arguments={"member_ids": [2], "text": "Synthetic"},
            )
            self.assertEqual(json.loads(result)["error"], "Bots can't receive DMs.")

    async def test_other_recipient_footer_uses_asker_when_cache_entry_is_missing(self):
        self.members.pop(1)
        with patch(
            "elbow_helper.features.agent.discord_actions.direct_messages.has_access_requirements",
            return_value=True,
        ):
            await deliver_dms(self.context, [2], "Synthetic")
        self.assertEqual(
            self.members[2].send.await_args.args,
            ("Synthetic\n\nSynthetic 1 asked me to send this.",),
        )

    def setUp(self):
        self.members = {
            identifier: SimpleNamespace(
                id=identifier, display_name=f"Synthetic {identifier}",
                mention=f"<@{identifier}>", bot=False, send=AsyncMock(),
            )
            for identifier in (1, 2)
        }
        self.context = SimpleNamespace(
            guild=SimpleNamespace(get_member=self.members.get), member=self.members[1],
            state=AgentTurnState(),
        )

    async def test_preview_and_confirmed_delivery(self):
        with (
            patch(
                "elbow_helper.features.agent.discord_actions.direct_messages."
                "require_evidence_access",
                AsyncMock(),
            ),
            patch(
                "elbow_helper.features.agent.discord_actions.direct_messages."
                "has_access_requirements",
                return_value=True,
            ),
            patch(
                "elbow_helper.features.agent.discord_actions.direct_messages."
                "resolve_post_attachment",
                AsyncMock(return_value=None),
            ),
            patch(
                "elbow_helper.features.agent.discord_actions.direct_messages.asyncio.sleep",
                AsyncMock(),
            ) as sleep,
        ):
            await prepare_direct_messages(
                self.context, {"member_ids": [1, 2], "text": "Synthetic message"},
            )
            action = self.context.state.proposed_changes[0]
            self.assertEqual(action.preview.lines, ("DM <@1>, <@2>:",))
            self.assertEqual(action.preview.details, ("Synthetic message",))
            self.assertEqual(action.preview.count, 2)
            self.members[1].send.assert_not_awaited()
            result = await action.run()
            self.assertEqual(self.members[1].send.await_args.args, ("Synthetic message",))
            self.assertEqual(
                self.members[2].send.await_args.args,
                ("Synthetic message\n\nSynthetic 1 asked me to send this.",),
            )
            sleep.assert_awaited_once_with(1)
            self.assertTrue(all(row["delivered"] for row in result.result["direct_messages"]))

    def test_recipient_and_lead_rules(self):
        self.assertEqual(dm_recipients(self.context, [1]), [self.members[1]])
        for identifiers in ([99], [1, 1], [], list(range(51))):
            with self.assertRaises(ActionRefused):
                dm_recipients(self.context, identifiers)
        self.members[2].bot = True
        with self.assertRaises(ActionRefused):
            dm_recipients(self.context, [2])
        self.members[2].bot = False
        with patch(
            "elbow_helper.features.agent.discord_actions.direct_messages.has_access_requirements",
            return_value=False,
        ), self.assertRaises(ActionRefused):
            dm_recipients(self.context, [2])

    async def test_closed_dms_and_run_report(self):
        for code in (50007, 50278):
            self.members[1].send.side_effect = discord.HTTPException(
                SimpleNamespace(status=403, reason="Forbidden"),
                {"code": code, "message": "Synthetic"},
            )
            outcomes = await deliver_dms(self.context, [1], "Synthetic")
            self.assertFalse(outcomes[0]["delivered"])
            report = format_run_report({
                "status": "completed",
                "steps": [{
                    "status": "completed", "action_class": "change", "action_label": "Send DM",
                    "outcome_json": json.dumps({"result": {"direct_messages": outcomes}}),
                }],
            })
            self.assertIn("DM to Synthetic 1", report)

    async def test_multiple_actions_share_cap_and_delivery_pacing(self):
        for identifier in range(3, 52):
            self.members[identifier] = SimpleNamespace(
                id=identifier, display_name=f"Synthetic {identifier}",
                mention=f"<@{identifier}>", bot=False, send=AsyncMock(),
            )
        with (
            patch(
                "elbow_helper.features.agent.discord_actions.direct_messages."
                "require_evidence_access",
                AsyncMock(),
            ),
            patch(
                "elbow_helper.features.agent.discord_actions.direct_messages."
                "has_access_requirements",
                return_value=True,
            ),
            patch(
                "elbow_helper.features.agent.discord_actions.direct_messages."
                "resolve_post_attachment",
                AsyncMock(return_value=None),
            ),
        ):
            await prepare_direct_messages(
                self.context, {"member_ids": list(range(1, 50)), "text": "Synthetic"},
            )
            with self.assertRaisesRegex(ActionRefused, "A confirmation can DM at most 50 members"):
                await prepare_direct_messages(
                    self.context, {"member_ids": [50, 51], "text": "Synthetic"},
                )
        with patch(
            "elbow_helper.features.agent.discord_actions.direct_messages.asyncio.sleep",
            AsyncMock(),
        ) as sleep:
            await deliver_dms(self.context, [1], "First")
            await deliver_dms(self.context, [1], "Second")
            sleep.assert_awaited_once_with(1)
