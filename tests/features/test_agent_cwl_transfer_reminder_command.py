"""Transfer reminder previews its published text and prior message removals."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.commands.adapters.cwl_transfer_reminder import (
    cwl_transfer_reminder_adapters, prepare_transfer_reminder,
)
from elbow_helper.features.cwl.transfers import CwlTransferMixin


class CwlTransferReminderCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_feature_builds_and_posts_the_prepared_content(self):
        workflow = CwlTransferMixin()
        workflow.clash_client = SimpleNamespace(configured=True)
        workflow.transfer_state = {"reminder_messages": []}
        workflow._transfer_reminder_lock = asyncio.Lock()
        workflow._save_transfer_state = MagicMock()
        workflow._cwl_spin_statuses = AsyncMock(return_value=(set(), set()))
        workflow._delete_transfer_reminder_messages = AsyncMock(return_value=[])
        workflow._native_roster_mismatches = MagicMock(return_value={"BEH": [3]})
        roster = SimpleNamespace(id=7, clan_code="BEH", guild_id=5)
        account = SimpleNamespace(
            player_tag="#P0Y", player_name="Player", clan_code="BEH",
            townhall=18, hero_sum=200,
        )
        workflow.roster_queries = SimpleNamespace(
            get=AsyncMock(return_value=roster),
            members=AsyncMock(return_value=[account]),
        )
        channel = SimpleNamespace(id=9)
        channel.send = AsyncMock(return_value=SimpleNamespace(id=99, channel=channel))
        workflow._resolve_clan_transfers_channel = AsyncMock(return_value=channel)
        with (
            patch.multiple(
                "elbow_helper.features.cwl.transfers",
                CWL_CLAN_ROSTER_IDS={"BEH": 7}, CWL_CLAN_CODES=("BEH",),
                CLAN_LINKS={"BEH": "https://example.test/clan"},
            ),
            patch("elbow_helper.features.cwl.transfers.fetch_account_profiles",
                  new=AsyncMock(return_value=([], set()))),
        ):
            prepared = await workflow.prepare_transfer_reminder(5)
            self.assertEqual(prepared["status"], "post")
            self.assertIn("<@3>", prepared["content"])
            channel.send.assert_not_awaited()
            result = await workflow.apply_transfer_reminder(
                prepared, enforce_state=True,
            )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["message_ids"], (99,))
        self.assertEqual(workflow.transfer_reminder_state()[0]["message_id"], 99)

    async def test_adapter_previews_all_reminder_pages_and_deletions(self):
        guild = SimpleNamespace(id=5, me=SimpleNamespace(id=1))
        member = SimpleNamespace(id=2)
        channel = SimpleNamespace(
            id=9, mention="<#9>", guild=guild,
            permissions_for=lambda _: SimpleNamespace(
                view_channel=True, send_messages=True,
            ),
        )
        prior = ({"channel_id": 9, "message_id": 88},)
        prepared = {
            "issue": None, "status": "post",
            "candidate_codes": ("BEH",),
            "mismatches": {"BEH": [3]},
            "content": "# Reminder\n<@3> move to BEH.",
            "chunks": ("# Reminder\n<@3> move to BEH.",),
            "channel": channel, "previous_entries": prior,
            "result_lines": (),
        }
        refs = prior

        async def apply(*_, **__):
            nonlocal refs
            refs = ({"channel_id": 9, "message_id": 99},)
            return {"status": "complete", "result_lines": ("Transfer reminder posted.",),
                    "message_ids": (99,)}

        workflow = SimpleNamespace(
            prepare_transfer_reminder=AsyncMock(return_value=prepared),
            apply_transfer_reminder=AsyncMock(side_effect=apply),
            transfer_reminder_state=lambda: refs,
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=guild, member=member,
        )
        change = await prepare_transfer_reminder(context, {})
        self.assertTrue(await change.preview.recheck())
        self.assertIn("Replace the CWL reminder in <#9>.",
                      change.preview.lines)
        self.assertIn("<@3> move to BEH.", change.preview.lines)
        workflow.apply_transfer_reminder.assert_not_awaited()
        result = await change.run()
        self.assertEqual(result.after["message_ids"], [99])
        self.assertIs(cwl_transfer_reminder_adapters()[0].classification,
                      ActionClass.CHANGE)


if __name__ == "__main__":
    unittest.main()
