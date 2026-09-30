"""Transfer queue changes are previewed before the feature changes state."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.commands.adapters.clan_transfers import (
    clan_transfer_adapters, prepare_transfer_cancel, prepare_transfer_request,
)


class TransferCommandTests(unittest.IsolatedAsyncioTestCase):
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
