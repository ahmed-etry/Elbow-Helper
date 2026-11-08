from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from datetime import timezone
from pathlib import Path
import sqlite3
from tests.temp_directory import TemporaryDirectory
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import discord
from discord import app_commands
from discord.ext import commands

from elbow_helper.infrastructure.discord.publishing import PublishingQueue
from elbow_helper.configuration.roles import HIBERNATING_ROLE_ID
from elbow_helper.features.rosters.services.accounts import RosterAccountDirectory
from elbow_helper.features.rosters.services.automation import RosterAutomationService
from elbow_helper.features.rosters.repository import RosterRepository
from elbow_helper.features.rosters.services.membership import RosterMembershipService
from elbow_helper.features.rosters.models import LinkedAccount
from elbow_helper.features.rosters.models import Roster
from elbow_helper.features.rosters.models import RosterLayout
from elbow_helper.features.rosters.models import RosterMember
from elbow_helper.features.rosters.cog import Rosters
from elbow_helper.features.rosters.services.posts import RosterPostService
from elbow_helper.features.rosters.services.profiles import _account_from_payload
from elbow_helper.features.rosters.services.profiles import RosterProfileService
from elbow_helper.features.rosters.services.publishing import RosterSheetPublisher
from elbow_helper.features.rosters.ui.rendering import build_roster_embeds
from elbow_helper.features.rosters.services.roles import RosterRoleSynchronizer
from elbow_helper.features.rosters.services.service import RosterDeleteCleanupError
from elbow_helper.features.rosters.services.service import RosterService
from elbow_helper.features.rosters.services.search import RosterSearchCache
from elbow_helper.features.rosters.services.scheduling import due_window
from elbow_helper.features.rosters.services.scheduling import next_window
from elbow_helper.features.rosters.services.scheduling import normalize_clock
from elbow_helper.features.rosters.services.scheduling import one_off_window
from elbow_helper.features.rosters.services.scheduling import parse_clock
from elbow_helper.features.rosters.services.scheduling import parse_day_rule
from elbow_helper.features.rosters.services.scheduling import schedule_window
from elbow_helper.features.rosters.ui.views import AccountPickerView
from elbow_helper.features.rosters.ui.views import RosterRemovalView
from elbow_helper.infrastructure.clash import ClashClient
from elbow_helper.infrastructure.exports import LocalExportStore
from elbow_helper.infrastructure.exports import WorkbookWriter
from tests.features.profile_source import profile_demand


def _roster(**values):
    return replace(Roster(
        id=1, guild_id=1, name="Synthetic signup", clan_code="FAMILY", role_id=None,
        max_members=500, min_townhall=None, buttons_hidden=False, status="closed",
        schedule_enabled=False, schedule_utc_offset=None, open_day=None, open_time=None,
        close_day=None, close_time=None, one_off_open_ts=None, one_off_close_ts=None,
        reset_on_open=True, active_cycle_id=None, last_open_cycle_key=None, last_close_cycle_key=None,
    ), **values)


def _membership_service(
    repository: RosterRepository,
    accounts,
    *,
    profiles=None,
    roles=None,
    refresh_posts=None,
):
    locks: dict[int, asyncio.Lock] = {}
    if roles is None:
        roles = MagicMock()
        roles.sync = AsyncMock(return_value=True)
    refresh_posts = (
        refresh_posts if refresh_posts is not None else AsyncMock()
    )
    return (
        RosterMembershipService(
            repository,
            accounts,
            profiles or profile_demand(ClashClient(None)),
            roles,
            lambda roster_id: locks.setdefault(roster_id, asyncio.Lock()),
            refresh_posts,
        ),
        roles,
        refresh_posts,
    )


def _wire_roster_service(
    cog,
    repository: RosterRepository,
    *,
    bot=None,
    profiles=None,
    google_publisher=None,
    accounts=None,
    roles=None,
    posts=None,
):
    bot = bot if bot is not None else MagicMock()
    profiles = profiles or profile_demand(ClashClient(None))
    if accounts is None:
        accounts = MagicMock()
        accounts.clan_badge_url.return_value = None
    if roles is None:
        roles = MagicMock()
        roles.sync = AsyncMock(return_value=True)
    if posts is None:
        posts = MagicMock()
        posts.refresh = AsyncMock()
        posts.disable_all = AsyncMock(return_value=())
        posts.prune_stale = AsyncMock()
        posts.restore_persistent_views = AsyncMock()
        posts.refresh_posts_after_emoji_load = AsyncMock()
        posts.remove_deleted_message = AsyncMock()
        posts.remove_deleted_messages = AsyncMock()
        posts.remove_deleted_channel = AsyncMock()
        posts.post_interaction_response = AsyncMock()
    locks: dict[int, asyncio.Lock] = {}
    lock_for = lambda roster_id: locks.setdefault(roster_id, asyncio.Lock())
    search = RosterSearchCache(repository)
    automation = RosterAutomationService(
        bot,
        repository,
        roles,
        lock_for,
        posts.refresh,
    )
    service = RosterService(
        repository,
        search,
        roles,
        posts,
        automation,
    )
    cog.bot = bot
    cog._repository = repository
    cog._roster_search = search
    cog._locks = locks
    cog._roles = roles
    cog.posts = posts
    cog.profiles = RosterProfileService(repository, profiles)
    cog.publisher = RosterSheetPublisher(
        bot,
        repository,
        cog.profiles,
        google_publisher or MagicMock(),
        MagicMock(),
        MagicMock(),
    )
    cog.automation = automation
    cog.service = service
    return service, automation, posts, roles


class RosterRepositoryTests(unittest.TestCase):

    def test_automation_event_claim_survives_restarts_and_failed_sends_can_retry(self) -> None:
        path = Path(self.enterContext(TemporaryDirectory())) / "rosters.sqlite3"
        repository = RosterRepository(path)
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Signup",
            clan_code="FAMILY",
            role_id=1209095400301133824,
            max_members=500,
        )

        self.assertTrue(
            repository.claim_automation_event(roster.id, "2026-07", "opening")
        )
        restarted = RosterRepository(path)
        self.assertFalse(
            restarted.claim_automation_event(roster.id, "2026-07", "opening")
        )

        restarted.release_automation_event(roster.id, "2026-07", "opening")
        self.assertTrue(
            restarted.claim_automation_event(roster.id, "2026-07", "opening")
        )

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.repository = RosterRepository(Path(self.temp_dir.name) / "rosters.sqlite3")
        self.roster = self.repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=123,
            max_members=2,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()


    def test_clone_copies_reusable_settings_without_live_state(self) -> None:
        source, _ = self.repository.start_cycle(self.roster.id, "2026-07")
        source = self.repository.update_roster(
            source.id,
            clan_code="BEH",
            min_townhall=10,
            buttons_hidden=1,
            schedule_enabled=1,
            schedule_utc_offset="UTC+03:00",
            open_day="last-2",
            open_time="12:00",
            close_day="11",
            close_time="23:00",
            one_off_open_ts=123,
            one_off_close_ts=456,
            reset_on_open=1,
        )
        source_layout = self.repository.update_layout(
            source.id,
            show_discord=False,
            show_clan=False,
            player_width=20,
            discord_width=12,
        )
        self.repository.add_post(source.id, 111, 222)
        self.repository.add_members(
            source.id,
            source.active_cycle_id,
            42,
            [
                {
                    "player_tag": "#PLAYER",
                    "player_name": "Player",
                    "clan_code": "BEH",
                    "townhall": 18,
                    "hero_sum": 500,
                }
            ],
            source.max_members,
            source.min_townhall,
        )

        clone = self.repository.clone_roster(source.id, name="BE4 CWL")

        self.assertEqual(clone.guild_id, source.guild_id)
        self.assertEqual(clone.clan_code, source.clan_code)
        self.assertEqual(clone.role_id, source.role_id)
        self.assertEqual(clone.max_members, source.max_members)
        self.assertEqual(clone.min_townhall, source.min_townhall)
        self.assertTrue(clone.buttons_hidden)
        self.assertTrue(clone.schedule_enabled)
        self.assertEqual(clone.schedule_utc_offset, "UTC+03:00")
        self.assertEqual(clone.open_day, "last-2")
        self.assertEqual(clone.open_time, "12:00")
        self.assertEqual(clone.close_day, "11")
        self.assertEqual(clone.close_time, "23:00")
        self.assertTrue(clone.reset_on_open)
        self.assertEqual(clone.status, "closed")
        self.assertEqual(self.repository.list_posts(clone.id), [])
        self.assertIsNone(clone.one_off_open_ts)
        self.assertIsNone(clone.one_off_close_ts)
        self.assertIsNone(clone.active_cycle_id)
        self.assertIsNone(clone.last_open_cycle_key)
        self.assertIsNone(clone.last_close_cycle_key)
        self.assertEqual(self.repository.list_members(clone.id, clone.active_cycle_id), [])
        self.assertEqual(self.repository.get_layout(clone.id), source_layout)

        overridden = self.repository.clone_roster(
            source.id,
            name="BE4 CWL Override",
            clan_code="BE4",
            role_id=456,
            max_members=30,
            min_townhall=0,
        )
        self.assertEqual(overridden.clan_code, "BE4")
        self.assertEqual(overridden.role_id, 456)
        self.assertEqual(overridden.max_members, 30)
        self.assertIsNone(overridden.min_townhall)
        self.assertEqual(overridden.schedule_utc_offset, source.schedule_utc_offset)
        self.assertEqual(overridden.open_day, source.open_day)
        self.assertTrue(overridden.buttons_hidden)
        self.assertEqual(self.repository.get_layout(overridden.id), source_layout)


    def test_roster_layout_rejects_widths_outside_display_limits(self) -> None:
        with self.assertRaises(ValueError):
            self.repository.update_layout(self.roster.id, player_width=25)
        with self.assertRaises(ValueError):
            self.repository.update_layout(self.roster.id, discord_width=6)

    def test_signup_is_account_level_unique_and_capacity_limited(self) -> None:
        roster, _ = self.repository.start_cycle(self.roster.id, "2026-07")
        accounts = [
            {
                "player_tag": "#A", "player_name": "One", "clan_code": "BEH",
                "townhall": 18, "hero_sum": 421,
            },
            {"player_tag": "#B", "player_name": "Two", "clan_code": "BE4", "townhall": 17},
            {"player_tag": "#C", "player_name": "Three", "clan_code": "BE1", "townhall": 16},
        ]
        added, total = self.repository.add_members(
            roster.id, roster.active_cycle_id, 10, accounts, roster.max_members
        )
        duplicate_added, duplicate_total = self.repository.add_members(
            roster.id, roster.active_cycle_id, 10, accounts, roster.max_members
        )

        self.assertEqual((added, total), (2, 2))
        self.assertEqual((duplicate_added, duplicate_total), (0, 2))
        self.assertEqual(
            [row.player_tag for row in self.repository.list_members(roster.id, roster.active_cycle_id)],
            ["#A", "#B"],
        )
        self.assertEqual(
            self.repository.list_members(roster.id, roster.active_cycle_id)[0].hero_sum,
            421,
        )

    def test_signup_has_no_unconfigured_townhall_restriction(self) -> None:
        roster, _ = self.repository.start_cycle(self.roster.id, "2026-07")
        added, total = self.repository.add_members(
            roster.id,
            roster.active_cycle_id,
            10,
            [{"player_tag": "#LOW", "player_name": "Low", "townhall": 7}],
            roster.max_members,
        )

        self.assertEqual((added, total), (1, 1))

    def test_configured_townhall_minimum_filters_signups(self) -> None:
        roster = self.repository.update_roster(self.roster.id, min_townhall=13)
        roster, _ = self.repository.start_cycle(roster.id, "2026-07")
        added, total = self.repository.add_members(
            roster.id,
            roster.active_cycle_id,
            10,
            [
                {"player_tag": "#LOW", "player_name": "Low", "townhall": 12},
                {"player_tag": "#OK", "player_name": "Ready", "townhall": 13},
            ],
            roster.max_members,
            roster.min_townhall,
        )

        self.assertEqual((added, total), (1, 1))
        self.assertEqual(
            [member.player_tag for member in self.repository.list_members(
                roster.id, roster.active_cycle_id
            )],
            ["#OK"],
        )


    def test_new_cycle_preserves_history_without_reusing_signups(self) -> None:
        first, _ = self.repository.start_cycle(self.roster.id, "2026-07")
        self.repository.add_members(
            first.id,
            first.active_cycle_id,
            10,
            [{"player_tag": "#A", "player_name": "One", "clan_code": "BEH", "townhall": 18}],
            first.max_members,
        )
        second, previous_cycle_id = self.repository.start_cycle(self.roster.id, "2026-08")

        self.assertEqual(previous_cycle_id, first.active_cycle_id)
        self.assertEqual(self.repository.list_members(second.id, second.active_cycle_id), [])
        self.assertEqual(
            len(self.repository.list_members(first.id, previous_cycle_id)),
            1,
        )

    def test_role_signup_claim_only_uses_the_active_cycle(self) -> None:
        first, _ = self.repository.start_cycle(self.roster.id, "2026-07")
        self.repository.add_members(
            first.id,
            first.active_cycle_id,
            10,
            [{"player_tag": "#P2", "player_name": "One", "townhall": 18}],
            first.max_members,
        )

        self.assertTrue(self.repository.role_has_signup(123, 10))

        self.repository.start_cycle(first.id, "2026-08")

        self.assertFalse(self.repository.role_has_signup(123, 10))

    def test_refresh_updates_cached_account_details_without_losing_known_townhall(self) -> None:
        roster, _ = self.repository.start_cycle(self.roster.id, "2026-07")
        self.repository.add_members(
            roster.id,
            roster.active_cycle_id,
            10,
            [{"player_tag": "#A", "player_name": "Old", "clan_code": "BEH", "townhall": 18}],
            roster.max_members,
        )
        self.repository.refresh_member_snapshots(
            roster.id,
            roster.active_cycle_id,
            {"#A": {"player_name": "New", "clan_code": "BE4", "townhall": 0}},
        )
        member = self.repository.list_members(roster.id, roster.active_cycle_id)[0]

        self.assertEqual(member.player_name, "New")
        self.assertEqual(member.clan_code, "BE4")
        self.assertEqual(member.townhall, 18)


class RosterScheduleTests(unittest.TestCase):

    def test_parsers_reject_invalid_values(self) -> None:
        self.assertEqual(parse_clock("09:30"), (9, 30))
        self.assertEqual(normalize_clock("9:05"), "09:05")
        self.assertIsNone(parse_clock("25:00"))
        self.assertEqual(parse_day_rule(18), "18")
        self.assertEqual(parse_day_rule("LAST"), "last")
        self.assertEqual(parse_day_rule("last-1"), "last-1")
        self.assertEqual(parse_day_rule("LAST-2"), "last-2")
        self.assertIsNone(parse_day_rule("-1"))
        self.assertIsNone(parse_day_rule("29"))
        self.assertIsNone(parse_day_rule("30"))
        self.assertIsNone(parse_day_rule("31"))
        self.assertIsNone(parse_day_rule("last-0"))
        self.assertIsNone(parse_day_rule("last-3"))
        self.assertIsNone(parse_day_rule("last-28"))
        self.assertIsNone(parse_day_rule("0"))


    def test_month_end_rollover_and_dst_boundaries(self):
        cases = (
            (2026, 4, "UTC", "last-2", "11:00", "last-1", "20:00", "2026-04-28T11:00+00:00", "2026-04-29T20:00+00:00"),
            (2026, 7, "UTC", "last-1", "11:00", "2", "20:00", "2026-07-30T11:00+00:00", "2026-08-02T20:00+00:00"),
            (2026, 7, "UTC", "last", "20:00", "last", "19:00", "2026-07-31T20:00+00:00", "2026-08-31T19:00+00:00"),
            (2026, 3, "Europe/Paris", "last-2", "02:30", "last-1", "02:30", "2026-03-29T01:30+00:00", "2026-03-30T00:30+00:00"),
            (2026, 10, "Europe/Paris", "25", "02:30", "26", "02:30", "2026-10-25T00:30+00:00", "2026-10-26T01:30+00:00"),
            (2026, 3, "Europe/Paris", 28, "11:00", "2", "20:00", "2026-03-28T10:00+00:00", "2026-04-02T18:00+00:00"),
            (2026, 2, "Asia/Beirut", 18, "09:00", "last-2", "21:00", "2026-02-18T07:00+00:00", "2026-02-26T19:00+00:00"),
        )
        for year, month, zone, opening, start, closing_day, end, expected_open, expected_close in cases:
            with self.subTest(month=month, zone=zone, opening=opening, closing=closing_day):
                window = schedule_window(year=year, month=month, timezone_name=zone,
                    open_day=opening, open_time=start, close_day=closing_day, close_time=end)
                self.assertIsNotNone(window)
                self.assertEqual(window.opens_at, datetime.fromisoformat(expected_open))
                self.assertEqual(window.closes_at, datetime.fromisoformat(expected_close))
                self.assertEqual(window.cycle_key, f"{year}-{month:02}")


    def test_one_off_window_uses_the_selected_timezone(self) -> None:
        window = one_off_window(
            opens_on="2026-07-20 09:00",
            closes_on="2026-07-30 21:00",
            timezone_name="Asia/Beirut",
        )

        self.assertIsNotNone(window)
        self.assertEqual(window.opens_at, datetime(2026, 7, 20, 6, 0, tzinfo=timezone.utc))
        self.assertEqual(window.closes_at, datetime(2026, 7, 30, 18, 0, tzinfo=timezone.utc))
        self.assertTrue(window.cycle_key.startswith("once:"))


    def test_due_window_supports_restart_catchup(self) -> None:
        roster = _roster(guild_id=1, name='CWL', clan_code='FAMILY', role_id=123, max_members=150)
        roster = replace(roster, schedule_enabled=True, schedule_utc_offset='UTC', open_day='18', open_time='11:00', close_day='last', close_time='20:00', reset_on_open=True)
        window = due_window(roster, datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(window.cycle_key, "2026-07")

    def test_next_window_returns_the_next_opening_after_a_cycle_closes(self) -> None:
        roster = _roster(guild_id=1, name='CWL', clan_code='FAMILY', role_id=123, max_members=500)
        roster = replace(roster, schedule_enabled=True, schedule_utc_offset='UTC', open_day='last-2', open_time='11:00', close_day='last-1', close_time='20:00', reset_on_open=True)

        window = next_window(
            roster,
            datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(window.cycle_key, "2026-08")
        self.assertEqual(window.opens_at, datetime(2026, 8, 29, 11, 0, tzinfo=timezone.utc))


class RosterRenderingTests(unittest.TestCase):


    def test_widest_layout_stays_within_discord_embed_limits(self) -> None:
        roster = _roster(guild_id=1, name='BEH War Signup', clan_code='BEH', role_id=None, max_members=500)
        members = [
            RosterMember(
                player_tag=f"#{index}",
                discord_user_id=index,
                player_name=f"Long Player Name {index}" * 2,
                clan_code="BEH",
                townhall=18,
                signed_up_ts=index,
                hero_sum=450,
            )
            for index in range(50)
        ]
        display_names = {
            index: f"long_discord_username_{index}" for index in range(50)
        }

        embed = build_roster_embeds(
            roster,
            members,
            display_names,
            layout=RosterLayout(player_width=24, discord_width=20),
        )[0]

        lines = embed.description.splitlines()
        self.assertTrue(all(len(line) == len(lines[0]) for line in lines))
        self.assertLessEqual(len(embed.description), 4096)
        self.assertLessEqual(len(embed), 6000)


class RosterDeletionTests(unittest.IsolatedAsyncioTestCase):
    def _roster_with_signup(self) -> tuple[RosterRepository, Roster]:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=123,
            max_members=50,
        )
        roster, _ = repository.start_cycle(roster.id, "2026-07")
        repository.add_members(
            roster.id,
            roster.active_cycle_id,
            42,
            [
                {
                    "player_tag": "#PLAYER",
                    "player_name": "Player",
                    "clan_code": "BEH",
                    "townhall": 18,
                    "hero_sum": 420,
                }
            ],
            roster.max_members,
        )
        return repository, roster

    @staticmethod
    def _service(repository, roles, posts, search=None) -> RosterService:
        return RosterService(
            repository,
            search or MagicMock(),
            roles,
            posts,
            MagicMock(),
        )

    async def test_roster_is_deleted_only_after_discord_cleanup_finishes(self) -> None:
        repository, roster = self._roster_with_signup()
        events: list[str] = []
        roles = MagicMock()
        roles.sync = AsyncMock(side_effect=lambda *args, **kwargs: events.append("role") or True)
        posts = MagicMock()
        posts.disable_all = AsyncMock(side_effect=lambda roster: events.append("posts") or ())
        search = MagicMock()
        original_delete = repository.delete_roster

        def delete_roster(roster_id: int) -> None:
            events.append("delete")
            original_delete(roster_id)

        repository.delete_roster = MagicMock(side_effect=delete_roster)
        service = self._service(repository, roles, posts, search)

        await service.delete(roster)

        self.assertEqual(events, ["role", "posts", "delete"])
        self.assertIsNone(repository.get_roster(roster.id))
        search.remove.assert_called_once_with(roster)

    async def test_each_delete_cleanup_failure_preserves_roster_and_signups(self):
        for failed_step in ('roles', 'posts'):
            with self.subTest(failed_step=failed_step):
                repository, roster = self._roster_with_signup()
                repository.add_post(roster.id, 111, 222)
                roles = MagicMock(sync=AsyncMock(return_value=failed_step != 'roles'))
                posts = MagicMock(disable_all=AsyncMock(return_value=(222,) if failed_step == 'posts' else ()))
                with self.assertRaises(RosterDeleteCleanupError) as raised:
                    await self._service(repository, roles, posts).delete(roster)
                self.assertIsNotNone(repository.get_roster(roster.id))
                self.assertEqual(len(repository.list_members(roster.id, roster.active_cycle_id)), 1)
                if failed_step == 'roles':
                    self.assertEqual(raised.exception.member_ids, (42,))
                    posts.disable_all.assert_not_awaited()
                else:
                    self.assertEqual(raised.exception.message_ids, (222,))


    async def test_post_cleanup_accepts_messages_that_are_already_gone(self) -> None:
        repository, roster = self._roster_with_signup()
        repository.add_post(roster.id, 111, 222)
        response = MagicMock(status=404, reason="Not Found")
        channel = MagicMock()
        channel.fetch_message = AsyncMock(
            side_effect=discord.NotFound(response, "missing")
        )
        bot = MagicMock(publishing=PublishingQueue())
        bot.get_channel.return_value = channel
        posts = RosterPostService(
            bot,
            repository,
            profile_demand(ClashClient(None)),
            MagicMock(),
            MagicMock(),
        )

        failed = await posts.disable_all(roster)

        self.assertEqual(failed, ())
        self.assertEqual(repository.list_posts(roster.id), [])


class RosterComponentTests(unittest.IsolatedAsyncioTestCase):
    class _Cog:
        pass


    async def test_roster_create_distinguishes_name_conflicts_from_other_failures(self) -> None:
        cog = object.__new__(Rosters)
        cog._require_lead = AsyncMock(return_value=True)
        repository = MagicMock()
        _wire_roster_service(cog, repository)
        interaction = MagicMock()
        interaction.guild_id = 1
        clan = app_commands.Choice(name="BEH - Hellbow", value="BEH")

        with (
            patch("elbow_helper.features.rosters.cog.warn", new=AsyncMock()) as mocked_warn,
            patch("elbow_helper.features.rosters.cog.LOGGER.exception"),
        ):
            repository.create_roster.side_effect = sqlite3.IntegrityError(
                "UNIQUE constraint failed: rosters.guild_id, rosters.name"
            )
            await cog.roster_create(interaction, "Test Roster", clan)
            mocked_warn.assert_awaited_once_with(
                interaction,
                "A roster with that name already exists.",
            )

        with (
            patch("elbow_helper.features.rosters.cog.warn", new=AsyncMock()) as mocked_warn,
            patch("elbow_helper.features.rosters.cog.LOGGER.exception"),
        ):
            repository.create_roster.side_effect = sqlite3.IntegrityError(
                "NOT NULL constraint failed: rosters.min_townhall"
            )
            await cog.roster_create(interaction, "Test Roster", clan)
            mocked_warn.assert_awaited_once_with(
                interaction,
                "The roster couldn't be created.",
            )


    async def test_roster_clone_applies_selected_overrides(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        source = repository.create_roster(
            guild_id=1,
            name="BEH CWL",
            clan_code="BEH",
            role_id=123,
            max_members=50,
            min_townhall=16,
        )
        cog = object.__new__(Rosters)
        _wire_roster_service(cog, repository)
        cog._require_lead = AsyncMock(return_value=True)
        cog._resolve_roster = AsyncMock(return_value=source)
        interaction = MagicMock(guild_id=1)
        interaction.response.send_message = AsyncMock()
        clan = app_commands.Choice(name="BE4 - Hellbow 4", value="BE4")
        signup_role = MagicMock(id=456)

        await cog.roster_clone(
            interaction,
            str(source.id),
            "BE4 CWL",
            clan,
            signup_role,
            30,
            0,
        )

        clone = next(row for row in repository.list_rosters(1) if row.name == "BE4 CWL")
        self.assertEqual(clone.clan_code, "BE4")
        self.assertEqual(clone.role_id, 456)
        self.assertEqual(clone.max_members, 30)
        self.assertIsNone(clone.min_townhall)
        interaction.response.send_message.assert_awaited_once_with(
            "Created **BE4 CWL** from **BEH CWL**.",
            ephemeral=True,
        )


    async def test_refresh_updates_every_registered_roster_post(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )
        repository.add_post(roster.id, 111, 222)
        repository.add_post(roster.id, 333, 444)
        first_message = MagicMock(components=[])
        first_message.edit = AsyncMock()
        second_message = MagicMock(components=[])
        second_message.edit = AsyncMock()
        cog = object.__new__(Rosters)
        posts = RosterPostService(
            MagicMock(publishing=PublishingQueue()),
            repository,
            profile_demand(ClashClient(None)),
            MagicMock(),
            cog,
        )
        posts.fetch = AsyncMock(side_effect=[first_message, second_message])
        posts.render = AsyncMock(return_value=([MagicMock()], 0, 1))
        posts.message_view = MagicMock(return_value=MagicMock())

        await posts.refresh(roster)

        first_message.edit.assert_awaited_once()
        second_message.edit.assert_awaited_once()

    async def test_refresh_retires_post_buried_by_one_hundred_messages(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )
        repository.add_post(roster.id, 111, 222)

        class ActiveChannel:
            id = 111
            last_message_id = 999

            async def history(self, **kwargs):
                self.history_options = kwargs
                for _ in range(100):
                    yield object()

        channel = ActiveChannel()
        message = MagicMock(id=222, components=[])
        message.channel = channel
        message.edit = AsyncMock()
        bot = MagicMock(spec=commands.Bot, publishing=PublishingQueue())
        posts = RosterPostService(
            bot,
            repository,
            profile_demand(ClashClient(None)),
            MagicMock(),
            object.__new__(Rosters),
        )
        view = MagicMock(spec=discord.ui.View)
        posts._persistent_views[message.id] = view
        posts.fetch = AsyncMock(return_value=message)
        posts.render = AsyncMock()

        await posts.refresh(roster)

        message.edit.assert_awaited_once_with(view=None)
        view.stop.assert_called_once_with()
        posts.render.assert_not_awaited()
        self.assertEqual(repository.list_posts(roster.id), [])
        self.assertEqual(channel.history_options["limit"], 100)
        self.assertIs(channel.history_options["after"], message)

    async def test_refresh_keeps_post_with_fewer_than_one_hundred_newer_messages(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )
        repository.add_post(roster.id, 111, 222)

        class QuietChannel:
            id = 111
            last_message_id = 999
            history_calls = 0

            async def history(self, **kwargs):
                self.history_calls += 1
                for _ in range(99):
                    yield object()

        channel = QuietChannel()
        message = MagicMock(id=222, components=[])
        message.channel = channel
        message.edit = AsyncMock()
        posts = RosterPostService(
            MagicMock(publishing=PublishingQueue()),
            repository,
            profile_demand(ClashClient(None)),
            MagicMock(),
            object.__new__(Rosters),
        )
        posts.fetch = AsyncMock(return_value=message)
        posts.render = AsyncMock(return_value=([MagicMock()], 0, 1))
        posts.message_view = MagicMock(return_value=MagicMock())

        posts._bot.publishing = MagicMock(wraps=posts._bot.publishing)
        await posts.refresh(roster)
        await posts.refresh(roster)

        self.assertEqual(posts._bot.publishing.edit_message.call_count, 2)
        self.assertEqual(message.edit.await_count, 1)
        self.assertEqual(posts.render.await_count, 2)
        self.assertEqual(channel.history_calls, 1)
        self.assertEqual(len(repository.list_posts(roster.id)), 1)

    async def test_burial_scan_resumes_from_cached_progress(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )
        repository.add_post(roster.id, 111, 222)

        class ActiveChannel:
            id = 111
            last_message_id = 999

            def __init__(self):
                self.batch_sizes = iter((99, 1))
                self.history_options = []

            async def history(self, **kwargs):
                self.history_options.append(kwargs)
                for _ in range(next(self.batch_sizes)):
                    yield object()

        channel = ActiveChannel()
        message = MagicMock(id=222, components=[])
        message.channel = channel
        message.edit = AsyncMock()
        posts = RosterPostService(
            MagicMock(publishing=PublishingQueue()),
            repository,
            profile_demand(ClashClient(None)),
            MagicMock(),
            object.__new__(Rosters),
        )
        posts.fetch = AsyncMock(return_value=message)
        posts.render = AsyncMock(return_value=([MagicMock()], 0, 1))
        posts.message_view = MagicMock(return_value=MagicMock())

        await posts.refresh(roster)
        channel.last_message_id = 1000
        await posts.refresh(roster)

        self.assertEqual(len(channel.history_options), 2)
        self.assertEqual(channel.history_options[1]["limit"], 1)
        self.assertEqual(channel.history_options[1]["after"].id, 999)
        posts.render.assert_awaited_once()
        self.assertEqual(repository.list_posts(roster.id), [])

    async def test_reconciliation_removes_only_ineligible_active_members(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        first = repository.create_roster(
            guild_id=1,
            name="First",
            clan_code="FAMILY",
            role_id=123,
            max_members=500,
        )
        first, _ = repository.start_cycle(first.id, "old")
        repository.add_members(
            first.id,
            first.active_cycle_id,
            42,
            [{"player_tag": "#OLD", "player_name": "Old", "townhall": 18}],
            first.max_members,
        )
        old_cycle_id = first.active_cycle_id
        first, _ = repository.start_cycle(first.id, "current")
        repository.add_members(
            first.id,
            first.active_cycle_id,
            42,
            [{"player_tag": "#A", "player_name": "A", "townhall": 18}],
            first.max_members,
        )
        repository.add_members(
            first.id,
            first.active_cycle_id,
            99,
            [{"player_tag": "#KEEP", "player_name": "Keep", "townhall": 18}],
            first.max_members,
        )
        second = repository.create_roster(
            guild_id=1,
            name="Second",
            clan_code="BEH",
            role_id=456,
            max_members=50,
        )
        second, _ = repository.start_cycle(second.id, "current")
        repository.add_members(
            second.id,
            second.active_cycle_id,
            42,
            [{"player_tag": "#B", "player_name": "B", "townhall": 17}],
            second.max_members,
        )
        service, roles, refresh_posts = _membership_service(
            repository,
            MagicMock(),
        )

        removed = await service.reconcile_eligible_members(1, {99})

        self.assertEqual(removed, 2)
        self.assertEqual(
            [
                member.player_tag
                for member in repository.list_members(
                    first.id,
                    first.active_cycle_id,
                )
            ],
            ["#KEEP"],
        )
        self.assertEqual(repository.list_members(second.id, second.active_cycle_id), [])
        self.assertEqual(
            [member.player_tag for member in repository.list_members(first.id, old_cycle_id)],
            ["#OLD"],
        )
        self.assertEqual(roles.sync.await_count, 2)
        self.assertEqual(refresh_posts.await_count, 2)


    async def test_ready_retries_before_reconciling_an_incomplete_member_cache(self) -> None:
        cog = object.__new__(Rosters)
        cog._membership_reconciled = False
        cog.posts = MagicMock()
        cog.posts.refresh_posts_after_emoji_load = AsyncMock()
        cog.membership = MagicMock()
        cog.membership.reconcile_eligible_members = AsyncMock()
        guild = MagicMock(id=1, unavailable=False, chunked=False, members=[])
        guild.chunk = AsyncMock(side_effect=discord.ClientException("no members intent"))
        cog.bot = MagicMock(guilds=[guild])

        await cog.on_ready()

        guild.chunk.assert_awaited_once_with(cache=True)
        cog.membership.reconcile_eligible_members.assert_not_awaited()
        self.assertFalse(cog._membership_reconciled)

        active = MagicMock(id=10, roles=[MagicMock(id=123)])
        guild.chunk = AsyncMock(return_value=[active])

        await cog.on_ready()

        cog.membership.reconcile_eligible_members.assert_awaited_once_with(
            1,
            {10},
        )
        self.assertTrue(cog._membership_reconciled)


    async def test_member_events_remove_signups_only_on_leave_or_hibernation_entry(self) -> None:
        cog = object.__new__(Rosters)
        cog.membership = MagicMock()
        cog.membership.remove_ineligible_member = AsyncMock()
        guild = MagicMock(id=1)
        active_role = MagicMock(id=123)
        hibernating_role = MagicMock(id=HIBERNATING_ROLE_ID)
        before = MagicMock(id=42, guild=guild, roles=[active_role])
        after = MagicMock(id=42, guild=guild, roles=[active_role, hibernating_role])

        await cog.on_member_update(before, before)
        cog.membership.remove_ineligible_member.assert_not_awaited()

        await cog.on_member_update(before, after)
        cog.membership.remove_ineligible_member.assert_awaited_once_with(1, 42)

        cog.membership.remove_ineligible_member.reset_mock()
        await cog.on_member_remove(after)
        cog.membership.remove_ineligible_member.assert_awaited_once_with(1, 42)

    async def test_deleted_roster_message_is_unregistered(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )
        repository.add_post(roster.id, 111, 222)
        cog = object.__new__(Rosters)
        posts = MagicMock()
        posts.remove_deleted_message = AsyncMock(
            side_effect=lambda message_id: repository.remove_post(message_id)
        )
        cog.posts = posts
        payload = MagicMock(message_id=222)

        await cog.on_raw_message_delete(payload)

        self.assertEqual(repository.list_posts(roster.id), [])

    async def test_invalid_saved_channel_is_pruned_when_posts_are_checked(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )
        post = repository.add_post(roster.id, 111, 222)
        cog = object.__new__(Rosters)
        bot = MagicMock(publishing=PublishingQueue())
        bot.get_channel.return_value = object()
        posts = RosterPostService(
            bot,
            repository,
            profile_demand(ClashClient(None)),
            MagicMock(),
            cog,
        )

        message = await posts.fetch(post)

        self.assertIsNone(message)
        self.assertEqual(repository.list_posts(roster.id), [])


    async def test_persistent_roster_pagination_uses_repository_page_count(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )
        roster, _ = repository.start_cycle(roster.id, "2026-07")
        repository.add_members(
            roster.id,
            roster.active_cycle_id,
            42,
            [
                {
                    "player_tag": f"#{index}",
                    "player_name": f"Player {index}",
                    "clan_code": "BEH",
                    "townhall": 18,
                    "hero_sum": 400,
                }
                for index in range(51)
            ],
            roster.max_members,
        )
        repository.add_post(roster.id, 111, 222)
        repository.add_post(roster.id, 333, 444)
        cog = object.__new__(Rosters)
        bot = MagicMock(publishing=PublishingQueue())
        posts = RosterPostService(
            bot,
            repository,
            profile_demand(ClashClient(None)),
            MagicMock(),
            cog,
        )
        _wire_roster_service(cog, repository, bot=bot, posts=posts)

        await cog.cog_load()

        self.assertEqual(bot.add_view.call_count, 2)
        self.assertEqual(
            {call.kwargs["message_id"] for call in bot.add_view.call_args_list},
            {222, 444},
        )
        view = bot.add_view.call_args.args[0]
        labels = [item.label for item in view.children]
        self.assertIn("Page 1/2", labels)
        self.assertNotIn("Page 1/3", labels)


    async def test_layout_changes_persist_and_refresh_every_post(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="BEH War Signup",
            clan_code="BEH",
            role_id=None,
            max_members=500,
        )
        cog = object.__new__(Rosters)
        _, _, posts, _ = _wire_roster_service(cog, repository)
        cog.is_lead = MagicMock(return_value=True)
        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.edit_original_response = AsyncMock()

        await cog.update_roster_layout_columns(
            interaction,
            roster.id,
            {"townhall", "clan"},
        )
        await cog.update_roster_layout_widths(
            interaction,
            roster.id,
            player_width=18,
            discord_width=12,
        )

        self.assertEqual(
            repository.get_layout(roster.id),
            RosterLayout(
                show_townhall=True,
                show_discord=False,
                show_clan=True,
                player_width=18,
                discord_width=12,
            ),
        )
        self.assertEqual(posts.refresh.await_count, 2)
        self.assertEqual(interaction.response.defer.await_count, 2)
        self.assertEqual(interaction.edit_original_response.await_count, 2)
        self.assertEqual(
            interaction.edit_original_response.await_args_list[0].kwargs["content"],
            "Roster now shows Town Hall, Player, and Clan.\n"
            "Google Sheets keeps every column.",
        )
        self.assertEqual(
            interaction.edit_original_response.await_args_list[1].kwargs["content"],
            "Name lengths updated: Player 18, Discord 12.\n"
            "Google Sheets keeps every column.",
        )
        interaction.response.send_message.assert_not_called()
        interaction.followup.send.assert_not_called()


    async def test_roster_export_falls_back_to_discord_before_cleanup(self) -> None:
        with TemporaryDirectory() as directory:
            workbook = Path(directory) / "roster.xlsx"
            workbook.write_bytes(b"xlsx")
            report = SimpleNamespace(
                google_link=None,
                google_warning="Google Sheets isn't available here.",
                workbook_path=workbook,
                workbook_name="cwl_sign_up_roster.xlsx",
            )
            roster = SimpleNamespace(id=7, name="CWL Sign-up")
            message = MagicMock(
                attachments=[SimpleNamespace(url="https://discord.test/roster.xlsx")]
            )
            message.edit = AsyncMock()
            interaction = MagicMock()
            interaction.edit_original_response = AsyncMock(return_value=message)
            cog = object.__new__(Rosters)
            cog.publisher = MagicMock()
            cog.publisher.export = AsyncMock(return_value=(report, None))
            cog.publisher.discard = AsyncMock()

            await cog._send_roster_export(interaction, roster)

        upload = interaction.edit_original_response.await_args.kwargs
        self.assertEqual(upload["attachments"][0].filename, report.workbook_name)
        self.assertIn(report.google_warning, upload["content"])
        cog.publisher.discard.assert_awaited_once_with(report)

    async def test_roster_export_keeps_local_file_when_discord_delivery_fails(self) -> None:
        with TemporaryDirectory() as directory:
            workbook = Path(directory) / "roster.xlsx"
            workbook.write_bytes(b"xlsx")
            report = SimpleNamespace(
                google_link=None,
                google_warning=None,
                workbook_path=workbook,
                workbook_name="roster.xlsx",
            )
            roster = SimpleNamespace(id=7, name="CWL Sign-up")
            interaction = MagicMock()
            interaction.edit_original_response = AsyncMock(
                side_effect=RuntimeError("Discord unavailable")
            )
            cog = object.__new__(Rosters)
            cog.publisher = MagicMock()
            cog.publisher.export = AsyncMock(return_value=(report, None))
            cog.publisher.discard = AsyncMock()

            with self.assertRaises(RuntimeError):
                await cog._send_roster_export(interaction, roster)
            self.assertTrue(workbook.exists())

        cog.publisher.discard.assert_not_awaited()


    async def test_clear_workflow_removes_signups_roles_and_refreshes_posts(self) -> None:
        repository = RosterRepository(Path(self.enterContext(TemporaryDirectory())) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=123,
            max_members=500,
        )
        roster, _ = repository.start_cycle(roster.id, "2026-07")
        repository.add_members(
            roster.id,
            roster.active_cycle_id,
            10,
            [{"player_tag": "#A", "player_name": "Ahmad", "townhall": 18}],
            roster.max_members,
        )
        repository.add_members(
            roster.id,
            roster.active_cycle_id,
            11,
            [{"player_tag": "#B", "player_name": "Luna", "townhall": 17}],
            roster.max_members,
        )
        membership, roles, refresh_posts = _membership_service(
            repository,
            MagicMock(),
        )

        result = await membership.clear(roster.id)

        self.assertEqual(
            result.message,
            "Cleared current signups for 2 members from **CWL Sign-up**.",
        )
        self.assertEqual(
            repository.list_members(roster.id, roster.active_cycle_id),
            [],
        )
        self.assertEqual(roles.sync.await_count, 2)
        roles.sync.assert_any_await(roster, 10, should_have=False)
        roles.sync.assert_any_await(roster, 11, should_have=False)
        refresh_posts.assert_awaited_once_with(roster)


    async def test_roster_removal_keeps_selections_until_confirmation(self) -> None:
        members = [
            RosterMember(f"#{index}", index, f"Player {index}", "BEH", 18, index, 400)
            for index in range(26)
        ]
        view = RosterRemovalView(self._Cog(), 7, members, {}, selected_tags={"#0"})
        interaction = MagicMock()
        interaction.response.edit_message = AsyncMock()

        await view._change_page(interaction, 1)

        next_view = interaction.response.edit_message.await_args.kwargs["view"]
        self.assertEqual(next_view.selected_tags, {"#0"})
        remove = next(
            item
            for item in next_view.children
            if getattr(item, "label", None) == "Remove"
        )
        self.assertFalse(remove.disabled)


    async def test_staff_account_addition_waits_for_confirmation(self) -> None:
        account = LinkedAccount("#ABC", "Ahmad", "BEH", 18, 420)
        view = AccountPickerView(
            self._Cog(),
            7,
            member_id=10,
            accounts=[account],
            mode="signup",
            lead_override=True,
        )
        interaction = MagicMock()
        interaction.response.edit_message = AsyncMock()

        await view.select_accounts(interaction, ["#ABC"])

        add = next(
            item for item in view.children if getattr(item, "label", None) == "Add"
        )
        self.assertFalse(add.disabled)
        self.assertEqual(view.selected_tags, {"#ABC"})
        self.assertTrue(
            any(
                getattr(item, "placeholder", None) == "Select another member"
                for item in view.children
            )
        )
        self.assertTrue(
            any(getattr(item, "label", None) == "Bulk add" for item in view.children)
        )

        await view.deselect(interaction)

        self.assertFalse(view.selected_tags)
        self.assertTrue(add.disabled)
        deselect = next(
            item for item in view.children if getattr(item, "label", None) == "Deselect"
        )
        self.assertTrue(deselect.disabled)


    async def test_bulk_add_uses_linked_accounts_and_reports_unmatched_tags(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=123,
            max_members=500,
        )
        roster = repository.update_roster(roster.id, min_townhall=19)
        roster, _ = repository.start_cycle(roster.id, "2026-07")

        class _AccountLinks:
            @staticmethod
            def get_link_by_tag(tag: str):
                return {"player_tag": tag, "discord_user_id": 10} if tag == "#PYL" else None

            @staticmethod
            def get_links_for_user(member_id: int):
                return [{"player_tag": "#PYL", "discord_user_id": member_id}]

            @staticmethod
            def get_player_location(tag: str):
                if tag == "#PYL":
                    return {
                        "player_name": "Ahmad",
                        "clan_code": "BEH",
                        "townhall": 18,
                    }
                return None

            @staticmethod
            def get_clan_badge_url(_clan_code: str):
                return None

        cog = object.__new__(Rosters)
        membership, roles, _ = _membership_service(
            repository,
            RosterAccountDirectory(_AccountLinks()),
        )
        cog.membership = membership
        cog.is_lead = MagicMock(return_value=True)
        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.edit_original_response = AsyncMock()

        with patch(
            "elbow_helper.features.rosters.services.membership.enrich_accounts",
            new=AsyncMock(side_effect=lambda accounts, _client: accounts),
        ):
            await cog.bulk_add_roster_accounts(
                interaction,
                roster.id,
                "#PYL #Q2 BAD!",
            )

        members = repository.list_members(roster.id, roster.active_cycle_id)
        self.assertEqual([member.player_tag for member in members], ["#PYL"])
        result = interaction.edit_original_response.await_args.kwargs["content"]
        self.assertIn("Added 1 account", result)
        self.assertIn("1 player tag wasn't linked", result)
        self.assertIn("1 entry wasn't a valid player tag", result)
        roles.sync.assert_awaited_once_with(roster, 10, should_have=True)

    async def test_roster_edit_sets_and_removes_the_townhall_minimum(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )
        roster, _ = repository.start_cycle(roster.id, "2026-07")
        repository.add_members(
            roster.id,
            roster.active_cycle_id,
            10,
            [{"player_tag": "#LOW", "player_name": "Low", "townhall": 7}],
            roster.max_members,
        )
        cog = object.__new__(Rosters)
        _wire_roster_service(cog, repository)
        cog._require_lead = AsyncMock(return_value=True)
        cog._resolve_roster = AsyncMock(
            side_effect=lambda *_: repository.get_roster(roster.id)
        )

        interaction = MagicMock()
        interaction.response.send_message = AsyncMock()
        await cog.roster_edit(interaction, str(roster.id), min_townhall=8)
        self.assertEqual(repository.get_roster(roster.id).min_townhall, 8)
        self.assertEqual(
            [member.player_tag for member in repository.list_members(
                roster.id, roster.active_cycle_id
            )],
            ["#LOW"],
        )

        interaction = MagicMock()
        interaction.response.send_message = AsyncMock()
        await cog.roster_edit(interaction, str(roster.id), min_townhall=0)
        self.assertIsNone(repository.get_roster(roster.id).min_townhall)

    async def test_leadership_add_can_bypass_the_townhall_minimum(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
            min_townhall=18,
        )
        roster, _ = repository.start_cycle(roster.id, "2026-07")
        account = LinkedAccount("#LOW", "Low", "BEH", 12, 300)
        accounts = MagicMock()
        accounts.for_member.return_value = [account]
        cog = object.__new__(Rosters)
        cog.bot = SimpleNamespace()
        cog.membership = _membership_service(repository, accounts)[0]
        interaction = MagicMock()
        interaction.response.edit_message = AsyncMock()
        interaction.edit_original_response = AsyncMock()

        await cog.apply_account_selection(
            interaction,
            roster.id,
            member_id=10,
            player_tags=[account.player_tag],
            mode="signup",
            account_snapshots={account.player_tag: account},
            bypass_min_townhall=True,
        )

        self.assertEqual(
            [member.player_tag for member in repository.list_members(
                roster.id, roster.active_cycle_id
            )],
            ["#LOW"],
        )


    async def test_refresh_reloads_current_clan_for_every_signed_up_account(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )
        roster, _ = repository.start_cycle(roster.id, "2026-07")
        repository.add_members(
            roster.id,
            roster.active_cycle_id,
            10,
            [{
                "player_tag": "#ABC",
                "player_name": "Ahmad",
                "clan_code": "BEH",
                "townhall": 18,
                "hero_sum": 420,
            }],
            roster.max_members,
        )
        cog = object.__new__(Rosters)
        _, _, posts, roles = _wire_roster_service(cog, repository)
        cog._refresh_times = {}
        interaction = MagicMock()
        interaction.message.components = []
        interaction.response.defer = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        refreshed = LinkedAccount("#ABC", "Ahmad", "BE4", 18, 450)

        with patch(
            "elbow_helper.features.rosters.services.profiles.enrich_accounts",
            new=AsyncMock(return_value=[refreshed]),
        ) as mocked_enrich:
            await cog.handle_refresh(interaction, roster.id)

        requested = mocked_enrich.await_args.args[0]
        self.assertEqual([account.player_tag for account in requested], ["#ABC"])
        stored = repository.list_members(roster.id, roster.active_cycle_id)[0]
        self.assertEqual(stored.clan_code, "BE4")
        self.assertEqual(stored.hero_sum, 450)
        roles.sync.assert_awaited_once_with(roster, 10, should_have=True)
        posts.refresh.assert_awaited_once_with(roster)


    async def test_stale_roster_search_cache_remains_available_during_refresh_failure(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )
        cog = object.__new__(Rosters)
        cog._repository = MagicMock()
        cog._repository.list_rosters.side_effect = sqlite3.OperationalError("busy")
        cog._roster_search = RosterSearchCache(cog._repository)
        cog._roster_search._cache = {1: (0.0, (roster,))}
        interaction = MagicMock(guild_id=1)

        with patch("elbow_helper.features.rosters.services.search.LOGGER.exception"):
            choices = await cog.roster_autocomplete(interaction, "CWL")
            tasks = tuple(cog._roster_search._refresh_tasks.values())
            await asyncio.gather(*tasks)

        self.assertEqual([choice.name for choice in choices], ["CWL Sign-up"])

    async def test_roster_search_cache_tracks_renames_and_deletions(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="Original",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )
        cog = object.__new__(Rosters)
        cog._repository = repository
        cog._roster_search = RosterSearchCache(repository)
        cog._roster_search._set(1, [roster])
        renamed = replace(roster, name="Renamed")
        interaction = MagicMock(guild_id=1)

        cog._roster_search.upsert(renamed)
        renamed_choices = await cog.roster_autocomplete(interaction, "Renamed")
        old_choices = await cog.roster_autocomplete(interaction, "Original")
        cog._roster_search.remove(renamed)
        deleted_choices = await cog.roster_autocomplete(interaction, "Renamed")

        self.assertEqual([choice.name for choice in renamed_choices], ["Renamed"])
        self.assertEqual(old_choices, [])
        self.assertEqual(deleted_choices, [])

    async def test_older_search_refresh_cannot_overwrite_a_roster_rename(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="Original",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )
        cog = object.__new__(Rosters)
        cog._repository = MagicMock()
        cog._repository.list_rosters.return_value = [roster]
        cog._roster_search = RosterSearchCache(cog._repository)
        cog._roster_search._set(1, [roster])
        renamed = replace(roster, name="Renamed")

        cog._roster_search.upsert(renamed)
        await cog._roster_search._refresh(1, generation=0)

        cached_rows = cog._roster_search._cache[1][1]
        self.assertEqual([row.name for row in cached_rows], ["Renamed"])


    async def test_roster_opt_out_keeps_a_role_claimed_by_the_live_war_lineup(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="War Sign-up",
            clan_code="BEH",
            role_id=123,
            max_members=500,
        )
        role = MagicMock(id=123)
        member = MagicMock()
        member.roles = [role]
        member.remove_roles = AsyncMock()
        guild = MagicMock()
        guild.get_member.return_value = member
        guild.get_role.return_value = role
        war_manager = MagicMock()
        war_manager.war_lineup_needs_role.return_value = True
        cog = object.__new__(Rosters)
        cog.bot = MagicMock(publishing=PublishingQueue())
        cog.bot.get_guild.return_value = guild
        cog.role_synchronizer = RosterRoleSynchronizer(
            cog.bot,
            repository,
            war_manager.war_lineup_needs_role,
        )

        synced = await cog.role_synchronizer.sync(
            roster,
            10,
            should_have=False,
        )

        self.assertTrue(synced)
        war_manager.war_lineup_needs_role.assert_called_once_with(123, 10)
        member.remove_roles.assert_not_awaited()

    async def test_role_removal_is_complete_when_the_member_or_role_is_gone(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="War Sign-up",
            clan_code="BEH",
            role_id=123,
            max_members=500,
        )
        guild = MagicMock()
        bot = MagicMock(publishing=PublishingQueue())
        bot.get_guild.return_value = guild
        synchronizer = RosterRoleSynchronizer(
            bot,
            repository,
            MagicMock(return_value=False),
        )

        guild.get_member.return_value = None
        guild.get_role.return_value = MagicMock(id=123)
        self.assertTrue(
            await synchronizer.sync(roster, 10, should_have=False)
        )

        guild.get_member.return_value = MagicMock()
        guild.get_role.return_value = None
        self.assertTrue(
            await synchronizer.sync(roster, 10, should_have=False)
        )

    async def test_scheduled_open_updates_state_without_needing_a_post(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=1209095400301133824,
            max_members=500,
        )
        cog = object.__new__(Rosters)
        bot = MagicMock(publishing=PublishingQueue())
        _, automation, posts, _ = _wire_roster_service(
            cog,
            repository,
            bot=bot,
        )

        await automation.open_scheduled(roster, "2026-07")

        self.assertEqual(repository.get_roster(roster.id).status, "open")
        self.assertEqual(repository.list_posts(roster.id), [])
        opened_roster = repository.get_roster(roster.id)
        bot.dispatch.assert_called_once_with("roster_cycle_opened", opened_roster)
        posts.refresh.assert_awaited_once()

    async def test_disabling_a_schedule_needs_no_other_options(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )
        roster = repository.configure_schedule(
            roster.id,
            enabled=True,
            timezone_name="UTC",
            open_day="last-2",
            open_time="11:00",
            close_day="last-1",
            close_time="20:00",
            reset_on_open=False,
        )
        cog = object.__new__(Rosters)
        _wire_roster_service(cog, repository)
        cog._require_lead = AsyncMock(return_value=True)
        cog._resolve_roster = AsyncMock(return_value=roster)
        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        await cog.roster_schedule(interaction, str(roster.id), enabled=False)

        stored = repository.get_roster(roster.id)
        self.assertFalse(stored.schedule_enabled)
        self.assertEqual(stored.open_day, "last-2")
        self.assertEqual(stored.close_day, "last-1")
        self.assertFalse(stored.reset_on_open)
        interaction.followup.send.assert_awaited_once_with(
            "Disabled automatic scheduling for **CWL Sign-up**.",
            ephemeral=True,
        )

    async def test_schedule_updates_only_the_options_that_are_entered(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )
        roster = repository.configure_schedule(
            roster.id,
            enabled=False,
            timezone_name="UTC",
            open_day="18",
            open_time="9:00",
            close_day="last-1",
            close_time="20:00",
            reset_on_open=False,
        )
        cog = object.__new__(Rosters)
        _wire_roster_service(cog, repository)
        cog._require_lead = AsyncMock(return_value=True)
        cog._resolve_roster = AsyncMock(return_value=roster)
        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        fixed_now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
        with patch(
            "elbow_helper.features.rosters.cog.datetime",
            wraps=datetime,
        ) as mocked_datetime:
            mocked_datetime.now.return_value = fixed_now
            await cog.roster_schedule(
                interaction,
                str(roster.id),
                open_day="last-2",
            )

        stored = repository.get_roster(roster.id)
        self.assertTrue(stored.schedule_enabled)
        self.assertEqual(stored.open_day, "last-2")
        self.assertEqual(stored.open_time, "09:00")
        self.assertEqual(stored.close_day, "last-1")
        self.assertEqual(stored.close_time, "20:00")
        self.assertEqual(stored.schedule_utc_offset, "UTC")
        self.assertFalse(stored.reset_on_open)
        confirmation = interaction.followup.send.await_args.args[0]
        self.assertTrue(confirmation.startswith("Scheduled **CWL Sign-up**.\nNext window: <t:"))
        self.assertNotIn("UTC", confirmation)

    async def test_posting_before_a_future_schedule_keeps_the_roster_closed(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )
        roster = repository.configure_schedule(
            roster.id,
            enabled=True,
            timezone_name="UTC",
            open_day="18",
            open_time="11:00",
            close_day="last-1",
            close_time="20:00",
            reset_on_open=True,
        )
        cog = object.__new__(Rosters)
        _, automation, _, _ = _wire_roster_service(cog, repository)
        stored = await automation.ensure_open_cycle(
            roster,
            now=datetime(
                2026,
                7,
                10,
                12,
                0,
                tzinfo=timezone.utc,
            ),
        )

        self.assertEqual(stored.status, "closed")
        self.assertIsNone(stored.active_cycle_id)


    async def test_schedule_can_be_configured_before_a_roster_is_posted(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=1209095400301133824,
            max_members=500,
        )
        roster = repository.create_roster(
            guild_id=1,
            name="Event Sign-up",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )
        cog = object.__new__(Rosters)
        _wire_roster_service(cog, repository)
        cog._require_lead = AsyncMock(return_value=True)
        cog._resolve_roster = AsyncMock(return_value=roster)
        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        with patch("elbow_helper.features.rosters.cog.warn", new=AsyncMock()) as mocked_warn:
            await cog.roster_schedule(
                interaction,
                str(roster.id),
                open_day="18",
                open_time="11:00",
                close_day="last-1",
                close_time="20:00",
                timezone="UTC",
            )

        self.assertTrue(repository.get_roster(roster.id).schedule_enabled)
        self.assertEqual(repository.list_posts(roster.id), [])
        mocked_warn.assert_not_awaited()


    async def test_roster_export_creates_a_valid_xlsx_fallback(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )
        roster, _ = repository.start_cycle(roster.id, "2026-07")
        repository.add_members(
            roster.id,
            roster.active_cycle_id,
            10,
            [{
                "player_tag": "#ABC",
                "player_name": "Ahmad",
                "clan_code": "BEH",
                "townhall": 18,
                "hero_sum": 420,
            }],
            roster.max_members,
        )
        publisher = MagicMock()
        publisher.upload_workbook = AsyncMock(
            return_value=(None, "Google Sheets isn't available here.")
        )
        local_exports = LocalExportStore(Path(temp_dir.name) / "exports")
        sheet_publisher = RosterSheetPublisher(
            MagicMock(publishing=PublishingQueue()),
            repository,
            RosterProfileService(repository, profile_demand(ClashClient(None))),
            publisher,
            WorkbookWriter(),
            local_exports,
        )

        report, warning = await sheet_publisher.export(roster)

        self.assertIsNotNone(report)
        self.assertIsNone(warning)
        self.assertTrue(zipfile.is_zipfile(report.workbook_path))
        self.assertEqual(report.google_warning, "Google Sheets isn't available here.")
        await sheet_publisher.discard(report)
        self.assertFalse(report.workbook_path.exists())

    async def test_empty_roster_export_does_not_create_a_sheet(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )
        publisher = MagicMock()
        publisher.upload_workbook = AsyncMock()
        bot = MagicMock(publishing=PublishingQueue())
        profiles = RosterProfileService(repository, profile_demand(ClashClient(None)))
        sheet_publisher = RosterSheetPublisher(
            bot,
            repository,
            profiles,
            publisher,
            MagicMock(),
            LocalExportStore(Path(temp_dir.name)),
        )

        report, warning = await sheet_publisher.export(roster)

        self.assertIsNone(report)
        self.assertEqual(warning, "No accounts are signed up to **CWL Sign-up**.")
        publisher.upload_workbook.assert_not_awaited()


class RosterProfileTests(unittest.TestCase):

    def test_player_payload_clears_an_old_clan_after_the_account_leaves(self) -> None:
        account = LinkedAccount("#PYL", "Ahmad", "BEH", 18, 420)

        enriched = _account_from_payload(
            account,
            {
                "tag": "#PYL",
                "name": "Ahmad",
                "townHallLevel": 18,
                "heroes": [],
            },
        )

        self.assertEqual(enriched.clan_code, "")
        self.assertEqual(enriched.townhall, 18)
