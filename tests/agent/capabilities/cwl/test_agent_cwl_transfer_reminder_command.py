"""Transfer reminder previews its published text and prior message removals."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.agent.actions.contracts import ActionClass
from elbow_helper.agent.tool_support.adapters.cwl.transfer_reminder import (
    cwl_transfer_reminder_adapters, prepare_transfer_reminder,
)


class CwlTransferReminderCommandTests(unittest.IsolatedAsyncioTestCase):

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
        self.assertIn("<@3> move to BEH.", change.preview.details)
        workflow.apply_transfer_reminder.assert_not_awaited()
        result = await change.run()
        self.assertEqual(result.after["message_ids"], [99])
        self.assertIs(cwl_transfer_reminder_adapters()[0].classification,
                      ActionClass.CHANGE)


if __name__ == "__main__":
    unittest.main()
