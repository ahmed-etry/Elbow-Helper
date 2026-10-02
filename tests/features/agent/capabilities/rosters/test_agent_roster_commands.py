"""Roster command previews use the public roster workflow."""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from elbow_helper.features.agent.capabilities.rosters.setup_commands import prepare_roster_clone
from elbow_helper.features.agent.capabilities.rosters.setup_commands import prepare_roster_create
from elbow_helper.features.agent.capabilities.rosters.setup_commands import prepare_roster_delete
from elbow_helper.features.agent.capabilities.rosters.setup_commands import prepare_roster_edit
from elbow_helper.features.agent.capabilities.rosters.timing_commands import prepare_roster_timing
from elbow_helper.features.agent.capabilities.rosters.timing_commands import prepare_roster_schedule
from elbow_helper.features.agent.capabilities.rosters.post_commands import prepare_roster_post
from elbow_helper.features.agent.capabilities.rosters.post_commands import prepare_roster_export
from elbow_helper.features.agent.capabilities.rosters.setup_commands import roster_adapters
from elbow_helper.features.rosters.cog import Rosters


class RosterCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_previews_settings_and_verifies_roster(self):
        roster = SimpleNamespace(id=7, name="War Signup")
        workflow = SimpleNamespace(
            validate_roster_name=lambda name: " ".join(name.split()),
            roster_name_available=AsyncMock(return_value=True),
            create_roster=AsyncMock(return_value=roster),
            get_roster=AsyncMock(return_value=roster),
        )
        guild = SimpleNamespace(id=5, get_role=lambda _: None, me=None)
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow), guild=guild,
        )
        prepared = await prepare_roster_create(context, {
            "name": " War  Signup ", "clan": "BEH", "max_members": 30,
        })
        self.assertEqual(roster_adapters()[0].path, "/roster create")
        self.assertIn("Create roster **War Signup**.", prepared.preview.lines)
        self.assertIn("Maximum accounts: 30", prepared.preview.lines)
        workflow.create_roster.assert_not_awaited()
        self.assertTrue(await prepared.preview.recheck())
        result = await prepared.run()
        self.assertEqual(result.result["roster_id"], 7)
        workflow.create_roster.assert_awaited_once_with(
            guild_id=5, name="War Signup", clan_code="BEH",
            role_id=None, max_members=30,
        )

    async def test_clone_previews_copied_settings_and_starts_closed(self):
        source = SimpleNamespace(
            id=4, guild_id=5, name="Source", clan_code="BEH",
            role_id=None, max_members=40, min_townhall=15,
            buttons_hidden=True, schedule_enabled=True,
            schedule_utc_offset="Europe/Paris", open_day="last-2",
            open_time="11:00", close_day="last-1", close_time="20:00",
            reset_on_open=True,
        )
        clone = SimpleNamespace(id=8, name="New Roster")
        workflow = SimpleNamespace(
            validate_roster_name=lambda name: name.strip(),
            roster_name_available=AsyncMock(return_value=True),
            get_roster=AsyncMock(side_effect=lambda row_id: source if row_id == 4 else clone),
            roster_clone_settings=lambda source, **kwargs: Rosters.roster_clone_settings(
                None, source, **kwargs,
            ),
            clone_roster=AsyncMock(return_value=clone),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=SimpleNamespace(id=5, get_role=lambda _: None),
        )
        values = {"roster": "4", "name": "New Roster", "min_townhall": 0}
        prepared = await prepare_roster_clone(context, values)
        self.assertTrue(await prepared.preview.recheck())
        self.assertIn("Minimum Town Hall: None", prepared.preview.lines)
        self.assertIn("The new roster starts closed with no signups.",
                      prepared.preview.lines)
        self.assertEqual((await prepared.run()).result["roster_id"], 8)
        workflow.clone_roster.assert_awaited_once_with(
            source, name="New Roster", clan_code=None, role_id=None,
            max_members=None, min_townhall=0,
        )

    async def test_delete_previews_posts_and_all_history(self):
        roster = SimpleNamespace(id=4, guild_id=5, name="Old", role_id=None)
        deleted = False

        async def get_roster(_):
            return None if deleted else roster

        async def delete_roster(_):
            nonlocal deleted
            deleted = True

        state = {
            "roster": roster, "member_ids": (), "posts": ((9, 99),),
            "history": ((11, (("#P0Y", 2),)),),
        }
        workflow = SimpleNamespace(
            get_roster=AsyncMock(side_effect=get_roster),
            roster_deletion_state=AsyncMock(return_value=state),
            delete_roster=AsyncMock(side_effect=delete_roster),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=SimpleNamespace(id=5, get_role=lambda _: None,
                                  get_member=lambda _: None),
        )
        prepared = await prepare_roster_delete(context, {"roster": "4"})
        self.assertTrue(await prepared.preview.recheck())
        self.assertIn("Delete 1 signup across 1 cycle.", prepared.preview.lines)
        self.assertIn("Disable controls on the roster post in <#9>.",
                      prepared.preview.lines)
        workflow.delete_roster.assert_not_awaited()
        self.assertTrue((await prepared.run()).after["deleted"])
        self.assertFalse(await prepared.preview.recheck())

    async def test_edit_previews_old_and_new_values_and_refreshes_posts(self):
        roster = SimpleNamespace(
            id=4, guild_id=5, name="Signup", clan_code="BEH",
            role_id=None, max_members=40, min_townhall=15,
        )
        current = roster

        async def update(_, changes):
            nonlocal current
            current = SimpleNamespace(**{**vars(current), **changes})
            return current

        workflow = SimpleNamespace(
            get_roster=AsyncMock(side_effect=lambda _: current),
            roster_edit_changes=lambda **kwargs: Rosters.roster_edit_changes(
                None, **kwargs,
            ),
            roster_edit_state=AsyncMock(return_value={
                "roster": roster, "account_count": 2,
                "member_ids": (2,), "posts": ((9, 99),),
            }),
            update_roster_settings=AsyncMock(side_effect=update),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=SimpleNamespace(id=5, get_role=lambda _: None,
                                  get_member=lambda _: None),
        )
        prepared = await prepare_roster_edit(context, {
            "roster": "4", "max_members": 50, "min_townhall": 0,
        })
        self.assertTrue(await prepared.preview.recheck())
        self.assertIn("Maximum accounts: 40 to 50", prepared.preview.lines)
        self.assertIn("Minimum Town Hall: 15 to None", prepared.preview.lines)
        self.assertIn("Refresh the roster post in <#9>.", prepared.preview.lines)
        workflow.update_roster_settings.assert_not_awaited()
        result = await prepared.run()
        self.assertEqual(result.after["changes"], {
            "max_members": 50, "min_townhall": None,
        })
        self.assertFalse(await prepared.preview.recheck())

    async def test_clear_timing_uses_feature_plan_and_checks_result(self):
        roster = SimpleNamespace(
            id=4, guild_id=5, name="Signup", one_off_open_ts=100,
            one_off_close_ts=200, role_id=None,
        )
        current = roster

        async def apply(_, plan):
            nonlocal current
            self.assertTrue(plan["clear"])
            current = SimpleNamespace(**{
                **vars(current), "one_off_open_ts": None,
                "one_off_close_ts": None,
            })
            return current

        workflow = SimpleNamespace(
            get_roster=AsyncMock(side_effect=lambda _: current),
            plan_roster_timing=lambda _, **kwargs: {
                "issue": None, "clear": True, "window": None,
            },
            roster_edit_state=AsyncMock(return_value={
                "roster": roster, "account_count": 0,
                "member_ids": (), "posts": ((9, 99),),
            }),
            apply_roster_timing=AsyncMock(side_effect=apply),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=SimpleNamespace(id=5),
        )
        prepared = await prepare_roster_timing(context, {"roster": "4"})
        self.assertTrue(await prepared.preview.recheck())
        self.assertIn("Clear one-off timing for **Signup**.",
                      prepared.preview.lines)
        self.assertEqual((await prepared.run()).after["one_off_open_ts"], None)
        self.assertFalse(await prepared.preview.recheck())

    async def test_disable_schedule_previews_status_and_refresh(self):
        roster = SimpleNamespace(
            id=4, guild_id=5, name="Signup", role_id=None,
            schedule_enabled=True, open_day="last-2", open_time="11:00",
            close_day="last-1", close_time="20:00",
            schedule_utc_offset="Europe/Paris", reset_on_open=True,
        )
        current = roster

        async def apply(_, plan):
            nonlocal current
            current = SimpleNamespace(**{
                **vars(current), "schedule_enabled": False,
            })
            return current, "Disabled automatic scheduling for **Signup**."

        workflow = SimpleNamespace(
            get_roster=AsyncMock(side_effect=lambda _: current),
            plan_roster_schedule=lambda roster, **kwargs: Rosters.plan_roster_schedule(
                None, roster, **kwargs,
            ),
            roster_schedule_preview=lambda roster, plan: Rosters.roster_schedule_preview(
                None, roster, plan,
            ),
            roster_edit_state=AsyncMock(return_value={
                "roster": roster, "account_count": 0,
                "member_ids": (), "posts": ((9, 99),),
            }),
            apply_roster_schedule=AsyncMock(side_effect=apply),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=SimpleNamespace(id=5),
        )
        prepared = await prepare_roster_schedule(context, {
            "roster": "4", "enabled": False,
        })
        self.assertTrue(await prepared.preview.recheck())
        self.assertIn("Disable monthly scheduling for **Signup**.",
                      prepared.preview.lines)
        self.assertIn("Automatic scheduling: Yes to No", prepared.preview.lines)
        self.assertEqual((await prepared.run()).after["schedule_enabled"], False)
        self.assertFalse(await prepared.preview.recheck())

    async def test_post_names_channel_and_opens_roster_after_confirm(self):
        roster = SimpleNamespace(
            id=4, guild_id=5, name="Signup", status="closed",
            role_id=None, buttons_hidden=False,
        )
        member = SimpleNamespace(id=2)
        bot_member = SimpleNamespace(id=3)
        message = SimpleNamespace(id=99)
        channel = SimpleNamespace(
            id=9, mention="<#9>", send=AsyncMock(return_value=message),
            permissions_for=lambda _: SimpleNamespace(
                view_channel=True, send_messages=True,
            ),
        )
        guild = SimpleNamespace(
            id=5, me=bot_member, get_member=lambda _: None,
            get_channel_or_thread=lambda _: channel,
        )
        channel.guild = guild
        opened = SimpleNamespace(id=4, status="open")
        workflow = SimpleNamespace(
            get_roster=AsyncMock(return_value=roster),
            roster_edit_state=AsyncMock(return_value={
                "roster": roster, "account_count": 2,
                "member_ids": (), "posts": (),
            }),
            roster_post_effect=lambda _: {
                "opens": True, "starts_cycle": False,
                "clears_signups": False,
            },
            preview_roster_post=AsyncMock(return_value={
                "effect": {"opens": True, "starts_cycle": False,
                           "clears_signups": False},
                "rendered": ([discord.Embed(title="Signup")], 0, 1),
                "pages": ((discord.Embed(title="Signup"),),),
                "signature": ((repr(discord.Embed(title="Signup").to_dict()),),),
            }),
            post_roster=AsyncMock(return_value=(opened, message)),
            roster_post_registered=AsyncMock(return_value=True),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=guild, member=member,
            source_message=SimpleNamespace(channel=channel),
        )
        with patch("elbow_helper.features.agent.capabilities.rosters.post_commands.discord.TextChannel",
                   new=SimpleNamespace):
            prepared = await prepare_roster_post(context, {"roster": "4"})
            self.assertTrue(await prepared.preview.recheck())
            self.assertIn("Post **Signup** in <#9>.", prepared.preview.lines)
            self.assertIn("Roster page 1:", prepared.preview.lines)
            self.assertIn("Signup", prepared.preview.lines)
            workflow.post_roster.assert_not_awaited()
            result = await prepared.run()
        self.assertEqual(result.result["message_id"], 99)
        self.assertEqual(workflow.post_roster.await_args.args, (4, channel.send))
        self.assertEqual(workflow.post_roster.await_args.kwargs["rendered"][2], 1)

    async def test_export_previews_accounts_and_delivers_file_after_confirm(self):
        roster = SimpleNamespace(id=4, guild_id=5, name="Signup")
        report = SimpleNamespace(
            workbook_name="roster_signup_20260930.xlsx",
            google_warning=None,
        )
        plan = {
            "roster": roster, "timestamp": "20260930",
            "workbook_name": report.workbook_name,
            "accounts": (("#P0Y", 2),),
        }
        workflow = SimpleNamespace(
            get_roster=AsyncMock(return_value=roster),
            roster_export_plan=AsyncMock(return_value=plan),
            export_roster=AsyncMock(return_value=(report, None)),
            deliver_roster_export=AsyncMock(return_value=(None, b"workbook")),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=SimpleNamespace(id=5),
        )
        prepared = await prepare_roster_export(context, {"roster": "4"})
        self.assertTrue(await prepared.preview.recheck())
        self.assertIn("#P0Y linked to <@2>", prepared.preview.lines)
        workflow.export_roster.assert_not_awaited()
        result = await prepared.run()
        self.assertEqual(result.attachments[0].data, b"workbook")
        workflow.export_roster.assert_awaited_once_with(
            roster, timestamp="20260930",
        )


if __name__ == "__main__":
    unittest.main()
