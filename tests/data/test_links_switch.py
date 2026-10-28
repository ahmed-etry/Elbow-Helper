"""Wired workflow crashes, facade parity, and isolated startup failures."""

import asyncio
from contextlib import closing
import shutil
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from elbow_helper.app import create_bot
from elbow_helper.core.paths import ApplicationPaths
from elbow_helper.data.links import LinkService
from elbow_helper.data.store import FamilyStore
from elbow_helper.features.account_links.cog import AccountLinks
from elbow_helper.features.records.service import RecordService
from elbow_helper.features.rosters.services.accounts import RosterAccountDirectory
from elbow_helper.agent.capabilities.account_links.reads import read_linked_accounts
from tests.features import test_recruitment_acceptance as recruitment_fixture

from .link_fixtures import CLANS, FrozenLegacyLinks
from .test_people_import import _PeopleFixture, ProcessCrash, row


class LinkSwitchTests(_PeopleFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.service.startup(at=200)
        self.cog = AccountLinks(SimpleNamespace(link_service=self.service), SimpleNamespace(configured=False))
        self.cog._try_refresh_linked_boards = AsyncMock()
        self.cog.refresh_linked_boards = AsyncMock()
        self.member = SimpleNamespace(id=300, mention="<@300>")

    async def restart(self):
        self.service = LinkService(FamilyStore(self.store.path), self.legacy)
        await self.service.startup()
        self.cog.link_service = self.service

    async def test_refused_legacy_write_aborts_immediately_and_next_change_works(self):
        with patch.object(self.service, "_apply_legacy", side_effect=OSError("refused before commit")):
            with self.assertRaises(OSError):
                await self.cog.account_add_operation(self.member, "#V0")
        self.assertEqual((await self.rows("SELECT status FROM link_operations"))[0]["status"], "aborted")
        self.assertNotIn("#V0", await asyncio.to_thread(self.cog.get_all_links))
        await self.cog.account_add_operation(self.member, "#V0")
        self.assertEqual((await asyncio.to_thread(self.cog.get_link_by_tag, "#V0"))["discord_user_id"], 300)

    async def test_error_after_legacy_commit_finishes_intent_before_readers_resume(self):
        real_apply = self.service._apply_legacy
        def committed_then_failed(*args, **kwargs):
            real_apply(*args, **kwargs)
            raise OSError("failure after commit")
        with patch.object(self.service, "_apply_legacy", committed_then_failed):
            with self.assertRaises(OSError):
                await self.cog.account_add_operation(self.member, "#V0")
        self.assertEqual((await self.rows("SELECT status FROM link_operations"))[0]["status"], "complete")
        self.assertEqual((await asyncio.to_thread(self.cog.get_link_by_tag, "#V0"))["discord_user_id"], 300)
        await self.restart()
        self.assertEqual((await self.service.read_links())["#V0"]["discord_user_id"], 300)

    async def test_add_crash_between_commits_recovers_without_acknowledging_half_a_change(self):
        with patch.object(self.service, "_finish", side_effect=ProcessCrash()):
            with self.assertRaises(ProcessCrash):
                await self.cog.account_add_operation(self.member, "#V0")
        self.assertEqual((await self.rows("SELECT status FROM link_operations"))[0]["status"], "pending")
        self.cog._try_refresh_linked_boards.assert_not_awaited()
        await self.restart()
        self.assertEqual((await asyncio.to_thread(self.cog.get_link_by_tag, "#V0"))["discord_user_id"], 300)

    async def test_remove_crash_between_commits_recovers(self):
        with patch.object(self.service, "_finish", side_effect=ProcessCrash()):
            with self.assertRaises(ProcessCrash):
                await self.cog.account_remove_operation("#P0")
        self.assertEqual((await self.rows("SELECT status FROM link_operations"))[0]["status"], "pending")
        await self.restart()
        self.assertIsNone(await asyncio.to_thread(self.cog.get_link_by_tag, "#P0"))

    async def test_batch_undo_crash_between_commits_restores_every_row_on_restart(self):
        before = await self.service.read_links()
        await self.cog.account_add_operation(self.member, "#P0 #V0")
        with patch.object(self.service, "_finish", side_effect=ProcessCrash()):
            with self.assertRaises(ProcessCrash):
                await self.cog.restore_account_add({**before, "#V0": None})
        await self.restart()
        self.assertEqual(await self.service.read_links(), before)

    async def test_recruitment_crash_between_commits_reports_failure_then_recovers(self):
        owner, _, member, channel, *_ = recruitment_fixture.RecruitmentAcceptanceTests._flow()
        owner.account_links = self.cog
        with recruitment_fixture.RecruitmentAcceptanceTests._config():
            with patch.object(self.service, "_finish", side_effect=OSError("crash after legacy commit")):
                result = await owner.perform_accept(user=member, valid_clans=["BEH"], nickname="Synthetic member",
                    days=7, target_channel=channel, additional_notes=None, player_tags=["#V0"],
                    player_rows=[{"player_tag": "#V0", "player_name": "Synthetic account"}])
        self.assertIn("Clash accounts were not linked: `#V0`.", result["failures"])
        self.assertEqual((await self.rows("SELECT status FROM link_operations"))[0]["status"], "pending")
        self.assertEqual((await asyncio.to_thread(self.cog.get_link_by_tag, "#V0"))["discord_user_id"], member.id)
        await self.restart()
        self.assertEqual((await self.service.read_links())["#V0"]["discord_user_id"], member.id)

    async def test_recruitment_process_crash_recovers_after_restart_without_clash(self):
        owner, _, member, channel, *_ = recruitment_fixture.RecruitmentAcceptanceTests._flow()
        owner.account_links = self.cog
        with recruitment_fixture.RecruitmentAcceptanceTests._config():
            with patch.object(self.service, "_finish", side_effect=ProcessCrash()):
                with self.assertRaises(ProcessCrash):
                    await owner.perform_accept(user=member, valid_clans=["BEH"], nickname="Synthetic member",
                        days=7, target_channel=channel, additional_notes=None, player_tags=["#V0"],
                        player_rows=[{"player_tag": "#V0", "player_name": "Synthetic account"}])
        self.assertEqual((await self.rows("SELECT status FROM link_operations"))[0]["status"], "pending")
        await self.restart()
        self.assertEqual((await self.service.read_links())["#V0"]["discord_user_id"], member.id)
        self.assertEqual((await self.rows("SELECT status FROM link_operations"))[0]["status"], "complete")

    async def test_every_facade_consumer_keeps_rows_and_order(self):
        reference = FrozenLegacyLinks(self.legacy)
        consumers = (
            ("recruitment", "get_links_by_tags", (["#Q0", "#P0", "#V0", "#Q0"],)),
            ("roster accounts", "get_links_for_user", (100,)),
            ("CWL roster and transfer analysis", "get_links_for_user", (200,)),
            ("missing Elder", "get_all_links", ()),
            ("war roles", "get_all_links", ()),
            ("records export", "get_links_for_user", (100,)),
            ("ranked population", "get_all_links", ()),
            ("agent member accounts", "get_links_for_members", ([200, 100, 999],)),
            ("agent account owners", "get_links_by_tags", (["#Q0", "#P0", "#V0"],)),
        )
        for mirrored in (True, False):
            self.service._started = mirrored
            for consumer, method, arguments in consumers:
                with self.subTest(consumer=consumer, mirrored=mirrored):
                    before = await asyncio.to_thread(getattr(reference, method), *arguments)
                    after = await asyncio.to_thread(getattr(self.cog, method), *arguments)
                    self.assertEqual(after, before)
                    if isinstance(before, dict):
                        self.assertEqual(tuple(after), tuple(before))
        # Exercise concrete adapters as well as their unchanged source contracts.
        reference.get_player_location = self.cog.get_player_location
        old_directory, new_directory = RosterAccountDirectory(reference), RosterAccountDirectory(self.cog)
        self.assertEqual(await asyncio.to_thread(new_directory.for_member, 100),
                         await asyncio.to_thread(old_directory.for_member, 100))
        old_records, new_records = object.__new__(RecordService), object.__new__(RecordService)
        old_records._account_links, new_records._account_links = reference, self.cog
        records = [{"member_id": 100}, {"member_id": 200}, {"member_id": 999}]
        self.assertEqual(await asyncio.to_thread(new_records.links_for, records),
                         await asyncio.to_thread(old_records.links_for, records))
        with patch("elbow_helper.agent.capabilities.account_links.reads.require_evidence_access", new=AsyncMock()):
            old_tool = await read_linked_accounts(SimpleNamespace(account_links=reference), {"member_ids": [100, 200, 999]})
            new_tool = await read_linked_accounts(SimpleNamespace(account_links=self.cog), {"member_ids": [100, 200, 999]})
        self.assertEqual(new_tool, old_tool)

    async def test_read_order_matches_sqlite_for_unicode_null_empty_and_case(self):
        await self.service.restore_links({"#P0": row(name="é"), "#Q0": row("#Q0", name="Z"),
            "#R0": row("#R0", 100, "a\0suffix"), "#V0": row("#V0", 100, "A"),
            "#G0": row("#G0", 100, ""), "#C0": row("#C0", 100, None)})
        reference = FrozenLegacyLinks(self.legacy)
        self.assertEqual(await asyncio.to_thread(self.cog.get_links_for_user, 100),
                         await asyncio.to_thread(reference.get_links_for_user, 100))

    async def test_undo_keeps_legacy_delete_then_insert_order_and_refresh_only_updates(self):
        original = await self.service.read_links()
        await self.cog.restore_account_add({"#Q0": original["#Q0"], "#P0": original["#P0"]})
        self.assertEqual(tuple(await self.service.read_links()), ("#R0", "#Q0", "#P0"))
        with closing(sqlite3.connect(self.legacy)) as connection, connection:
            connection.execute("CREATE TRIGGER no_insert_on_refresh BEFORE INSERT ON links "
                               "WHEN NEW.player_tag='#P0' BEGIN SELECT RAISE(ABORT,'unexpected insert'); END")
        await self.cog.update_link_last_seen(player_tag="#P0", player_name_last_seen="Synthetic refreshed name",
            last_seen_clan_tag="", last_seen_clan_code="", last_seen_role="member")
        self.assertEqual((await self.service.read_links())["#P0"]["player_name_last_seen"], "Synthetic refreshed name")

    async def test_unavailable_account_list_uses_existing_failure_handler_after_permission_check(self):
        self.cog.link_service = None
        self.cog._can_manage_accounts = MagicMock(return_value=True)
        interaction = SimpleNamespace(user=SimpleNamespace())
        with patch("elbow_helper.features.account_links.cog.fail", new=AsyncMock()) as failed:
            await AccountLinks.account_list.callback(self.cog, interaction, member=self.member)
            failed.assert_awaited_once_with(interaction)
        self.cog._can_manage_accounts.return_value = False
        with patch("elbow_helper.features.account_links.cog.deny", new=AsyncMock()) as denied:
            await AccountLinks.account_list.callback(self.cog, interaction, member=self.member)
            denied.assert_awaited_once_with(interaction)

    async def test_background_startup_never_bootstraps_or_blocks_consumers(self):
        for slow in (True, False):
            with self.subTest(slow=slow):
                project = self.root / ("slow" if slow else "deferred")
                paths = ApplicationPaths.from_project_root(project)
                destination = paths.data_root / "clan_links/links.sqlite3"
                destination.parent.mkdir(parents=True)
                shutil.copy2(self.legacy, destination)
                client = SimpleNamespace(get=AsyncMock(side_effect=OSError("Clash unavailable")))
                with patch("elbow_helper.app.CLANS", CLANS):
                    bot = create_bot(paths, client, SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
                loaded = []
                async def load(*args, **kwargs):
                    loaded.append("other features")
                    return SimpleNamespace()
                release = asyncio.Event()
                real_start = bot.link_service.startup
                async def start():
                    if slow:
                        await release.wait()
                    return await real_start()
                with (patch("elbow_helper.core.lifecycle.load_extensions", new=load),
                      patch("elbow_helper.core.lifecycle.sync_guild_commands", new=AsyncMock(return_value=())),
                      patch.object(bot.link_service, "startup", side_effect=start)):
                    await asyncio.wait_for(bot.setup_hook(), 2)
                    if slow:
                        self.assertFalse(bot._links_startup_task.done())
                    release.set()
                    await bot._links_startup_task
                client.get.assert_not_awaited()
                self.assertFalse(bot.link_service._started)
                self.assertEqual(len(await bot.link_service.read_links()), 3)
                self.assertEqual(loaded, ["other features"])
                self.assertEqual(bot.synced_command_count, 0)
                self.assertIs(bot.family_collectors.store, bot.link_service.store)
                self.assertEqual(len(bot.family_collectors._tasks), 9)
                await bot.close()
