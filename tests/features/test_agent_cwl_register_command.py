"""CWL registration previews the replaced thread and welcome post."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

from elbow_helper.features.agent.capabilities.cwl.thread_registration import (
    prepare_cwl_register, run_cwl_register,
)
from elbow_helper.features.cwl.config import CLAN_NAME_TO_CODE
from elbow_helper.features.cwl.threads.commands import CwlThreadCommandMixin


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
        self.assertTrue(any("CWL Thread Ready" in line for line in preview.lines))
        self.assertTrue(await preview.recheck())
        thread.send.assert_not_awaited()
        result = await run_cwl_register(context, values)
        self.assertEqual(result.visibility, "private")
        self.assertFalse(await preview.recheck())
        self.assertNotIn("10", workflow.data["threads"])
        self.assertIn("20", workflow.data["threads"])
        thread.send.assert_awaited_once()
        workflow.save_data.assert_called_once()
