from __future__ import annotations
from types import SimpleNamespace
import unittest

from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.capabilities.cwl.announcement import (
    cwl_announcement_adapters, prepare_roster_announcement,
)
from elbow_helper.features.cwl.announcements import PENDING_ROSTER_HUB_LINK
from elbow_helper.features.agent.capabilities.cwl.brief import (
    prepare_cwl_brief, run_cwl_brief,
)
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.cwl.announcements import CwlAnnouncementMixin
from unittest.mock import AsyncMock, MagicMock
import discord
from elbow_helper.features.agent.capabilities.cwl.thread_registration import (
    prepare_cwl_register, run_cwl_register,
)
from elbow_helper.features.cwl.config import CLAN_NAME_TO_CODE
from elbow_helper.features.cwl.threads.commands import CwlThreadCommandMixin


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
        self.assertTrue(any("link added when posted" in line
                            for line in change.preview.details))
        workflow.post_roster_announcement.assert_not_awaited()
        result = await change.run()
        self.assertEqual(result.result["message_ids"], [99])
        self.assertFalse(await change.preview.recheck())


class CwlBriefCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_previews_feature_content_and_posts_once(self):
        channel = SimpleNamespace(id=7, mention="#cwl-info")
        channel.permissions_for = lambda _: SimpleNamespace(
            view_channel=True, send_messages=True,
        )
        workflow = CwlAnnouncementMixin()
        workflow.bot = SimpleNamespace(get_channel=lambda _: channel)
        workflow._send_chunked = AsyncMock()
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=SimpleNamespace(me=SimpleNamespace(id=2)),
            member=SimpleNamespace(id=3), state=AgentTurnState(),
        )
        values = {
            "clan": "BEH", "mode": "highly_motivated",
            "helper_cwl": "@helper", "rotations": True,
        }
        preview = await prepare_cwl_brief(context, values)
        self.assertIn("#cwl-info", preview.lines[0])
        self.assertTrue(preview.details)
        self.assertTrue(await preview.recheck())
        workflow._send_chunked.assert_not_awaited()
        outcome = await run_cwl_brief(context, values)
        self.assertEqual(outcome.status, "complete")
        workflow._send_chunked.assert_awaited_once()
        self.assertEqual(workflow._send_chunked.await_args.args[0], channel)


class CwlRegistrationCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_previews_and_applies_feature_state(self):
        clan = next(iter(CLAN_NAME_TO_CODE))
        guild = SimpleNamespace(id=1, me=SimpleNamespace(id=4))
        thread = MagicMock(spec=discord.Thread)
        thread.id = 20
        thread.mention = "<#20>"
        thread.guild = guild
        thread.archived = False
        thread.locked = False
        thread.permissions_for.return_value = SimpleNamespace(
            view_channel=True, send_messages_in_threads=True,
        )
        thread.send = AsyncMock()
        workflow = CwlThreadCommandMixin()
        workflow.bot = SimpleNamespace(get_channel=lambda _: thread)
        workflow.data = {"threads": {"10": {"clan_name": clan}}}
        workflow.clan_configs = {clan: {"thread_id": 10}}
        workflow._drop_thread_registration = lambda thread_id: workflow.data["threads"].pop(thread_id)
        workflow._utc_now_iso = lambda: "2026-09-30T00:00:00+00:00"
        workflow.save_data = MagicMock()
        workflow.refresh_registered_cwl_status_for_clan = AsyncMock()
        workflow.cwl_registration_status_preview = AsyncMock(return_value={"kind": "none"})
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow), guild=guild,
            member=SimpleNamespace(id=5),
        )
        values = {"clan": clan, "thread_id": "20"}
        preview = await prepare_cwl_register(context, values)
        self.assertIn("Replace prior thread <#10>.", preview.lines)
        self.assertTrue(any("CWL Thread Ready" in line for line in preview.details))
        self.assertTrue(await preview.recheck())
        thread.send.assert_not_awaited()
        result = await run_cwl_register(context, values)
        self.assertEqual(result.visibility, "private")
        self.assertFalse(await preview.recheck())
        self.assertNotIn("10", workflow.data["threads"])
        self.assertIn("20", workflow.data["threads"])
        thread.send.assert_awaited_once()
        workflow.save_data.assert_called_once()
