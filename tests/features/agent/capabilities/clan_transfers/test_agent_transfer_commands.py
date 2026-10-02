"""Transfer queue changes are previewed before the feature changes state."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.features.agent.capabilities.clan_transfers.commands import (
    clan_transfer_adapters, prepare_transfer_cancel, prepare_transfer_request,
)
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.capabilities.clan_transfers.queue import prepare_clear_transfer_queue


class TransferCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_clear_queue_names_every_request_and_rechecks_before_running(self):
        state = {"clan_code": "BEH", "member_ids": (4, 5), "ping_message_id": 7,
                 "thread_id": 8, "board_channel_id": 9}
        workflow = SimpleNamespace(
            transfer_queue_clear_state=lambda clan: state,
            clear_transfer_queue=AsyncMock(return_value="Cleared two requests."),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda name: workflow),
            state=AgentTurnState(),
        )
        with patch("elbow_helper.features.agent.capabilities.clan_transfers.queue.require_evidence_access",
                   new_callable=AsyncMock):
            result = await prepare_clear_transfer_queue(context, {"clan_code": "BEH"})
        self.assertEqual(result["status"], "confirmation_required")
        action = context.state.command_proposals[0]
        self.assertTrue(any("<@4>" in line for line in action.preview.lines))
        self.assertTrue(any("<@5>" in line for line in action.preview.lines))
        self.assertTrue(any("<#8>" in line for line in action.preview.lines))
        self.assertTrue(await action.preview.recheck())
        workflow.clear_transfer_queue.assert_not_awaited()
        self.assertEqual((await action.run()).text, "Cleared two requests.")
        workflow.clear_transfer_queue.assert_awaited_once_with("BEH")

    async def test_request_and_cancel_use_same_queue_state(self):
        pending = False

        def preview(clan_code, member_id, *, cancel=False):
            if pending == cancel:
                return {
                    "issue": None, "clan_code": clan_code,
                    "member_id": member_id, "cancel": cancel,
                    "pending_count": int(pending), "thread_id": 9,
                    "notify_role_ids": (10,) if not cancel else (),
                }
            return {"issue": "No matching change"}

        async def request(*_):
            nonlocal pending
            pending = True
            return "Added"

        async def cancel(*_):
            nonlocal pending
            pending = False
            return "Cancelled"

        workflow = SimpleNamespace(
            transfer_request_preview=preview,
            transfer_request_status=lambda *_: pending,
            request_transfer=AsyncMock(side_effect=request),
            cancel_transfer=AsyncMock(side_effect=cancel),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            member=SimpleNamespace(id=2, mention="<@2>"),
        )
        values = {"destination": "BEH"}
        adapters = {adapter.path: adapter for adapter in clan_transfer_adapters()}
        self.assertEqual(set(adapters), {"/transfer request", "/transfer cancel"})
        request_change = await prepare_transfer_request(context, values)
        self.assertTrue(await request_change.preview.recheck())
        self.assertTrue(any("<@&10>" in line for line in request_change.preview.lines))
        workflow.request_transfer.assert_not_awaited()
        self.assertEqual((await request_change.run()).after["pending"], True)
        self.assertFalse(await request_change.preview.recheck())

        cancel_change = await prepare_transfer_cancel(context, values)
        self.assertTrue(await cancel_change.preview.recheck())
        self.assertEqual((await cancel_change.run()).after["pending"], False)
        self.assertFalse(await cancel_change.preview.recheck())


if __name__ == "__main__":
    unittest.main()
