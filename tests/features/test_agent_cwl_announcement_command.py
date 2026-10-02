"""CWL announcement previews include its post and placement release."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.capabilities.cwl.announcement import (
    cwl_announcement_adapters, prepare_roster_announcement,
)
from elbow_helper.features.cwl.announcements import PENDING_ROSTER_HUB_LINK


class CwlAnnouncementCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_announcement_previews_content_and_released_cycles(self):
        release = {}
        guild = SimpleNamespace(id=5, me=SimpleNamespace(id=1))
        channel = SimpleNamespace(
            id=9, mention="<#9>", guild=guild,
            permissions_for=lambda _: SimpleNamespace(
                view_channel=True, send_messages=True,
            ),
        )
        content = f"Rosters are posted! [CWL Rosters and Transfers]({PENDING_ROSTER_HUB_LINK})"
        prepared = {
            "issue": None, "content_preview": content,
            "content_template": content, "hub_url": None,
        }

        async def post(*_):
            release.update({"7": 8})
            return {"issue": None, "message": "The roster announcement is live.",
                    "messages": (SimpleNamespace(id=99),)}

        workflow = SimpleNamespace(
            prepare_roster_announcement=lambda **_: prepared,
            resolve_roster_announcement_channel=AsyncMock(return_value=channel),
            roster_announcement_cycles=AsyncMock(return_value={"7": 8}),
            roster_announcement_roster_names=AsyncMock(return_value=("CWL signup",)),
            roster_announcement_release_state=lambda: dict(release),
            post_roster_announcement=AsyncMock(side_effect=post),
            roster_announcement_released=lambda cycles: release == cycles,
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=guild, member=SimpleNamespace(id=2),
        )
        values = {"deadline_mode": "single", "deadline": "01-20:00",
                  "timezone": "Europe/Paris"}
        change = await prepare_roster_announcement(context, values)
        self.assertIs(cwl_announcement_adapters()[0].classification,
                      ActionClass.CHANGE)
        self.assertTrue(await change.preview.recheck())
        self.assertIn("**CWL signup**", change.preview.lines)
        self.assertTrue(any("link created on Confirm" in line
                            for line in change.preview.lines))
        workflow.post_roster_announcement.assert_not_awaited()
        result = await change.run()
        self.assertEqual(result.result["message_ids"], [99])
        self.assertFalse(await change.preview.recheck())


if __name__ == "__main__":
    unittest.main()
