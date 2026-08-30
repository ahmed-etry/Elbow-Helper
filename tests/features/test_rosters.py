from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import replace
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import ANY
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import discord
from discord import app_commands
import elbow_helper

from elbow_helper.discord.pagination import ADAPTIVE_JUMP_THRESHOLD
from elbow_helper.discord.pagination import FIRST_PAGE_LABEL
from elbow_helper.discord.pagination import LAST_PAGE_LABEL
from elbow_helper.discord.pagination import NEXT_PAGE_LABEL
from elbow_helper.discord.pagination import PREV_PAGE_LABEL
from elbow_helper.features.rosters.services.accounts import RosterAccountDirectory
from elbow_helper.features.rosters.services.automation import RosterAutomationService
from elbow_helper.features.rosters.repository import RosterRepository
from elbow_helper.features.rosters.repository.migrations import _create_supported_schema
from elbow_helper.features.rosters.ui.emojis import TownHallEmojiProvider
from elbow_helper.features.rosters.ui.emojis import TownHallEmojiSet
from elbow_helper.features.rosters.services.membership import account_count
from elbow_helper.features.rosters.services.membership import MembershipResult
from elbow_helper.features.rosters.services.membership import RosterMembershipService
from elbow_helper.features.rosters.models import LinkedAccount
from elbow_helper.features.rosters.models import Roster
from elbow_helper.features.rosters.models import RosterLayout
from elbow_helper.features.rosters.models import RosterMember
from elbow_helper.features.rosters.cog import Rosters
from elbow_helper.features.rosters.cog import _is_roster_name_conflict
from elbow_helper.features.rosters.services.posts import RosterPostService
from elbow_helper.features.rosters.services.profiles import _account_from_payload
from elbow_helper.features.rosters.services.profiles import RosterProfileService
from elbow_helper.features.rosters.services.publishing import RosterSheetPublisher
from elbow_helper.features.rosters.services.queries import RosterQueries
from elbow_helper.features.rosters.ui.rendering import build_roster_embeds
from elbow_helper.features.rosters.ui.rendering import roster_rows_per_page
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
from elbow_helper.features.rosters.ui.views import RosterMessageView
from elbow_helper.features.rosters.ui.views import RosterRemovalView
from elbow_helper.features.rosters.ui.views import RosterColumnWidthsModal
from elbow_helper.features.rosters.ui.views import RosterLayoutView
from elbow_helper.features.rosters.ui.views import RosterProgressView
from elbow_helper.features.rosters.ui.views import RosterSettingsView
from elbow_helper.features.rosters.ui.views import RosterTargetMemberView
from elbow_helper.infrastructure.time import fixed_utc_offset_name
from elbow_helper.infrastructure.time import resolve_timezone
from elbow_helper.infrastructure.clash import ClashClient
from elbow_helper.infrastructure.exports import LocalExportStore
from elbow_helper.infrastructure.exports import WorkbookWriter


def _membership_service(
    repository: RosterRepository,
    accounts,
    *,
    clash_client: ClashClient | None = None,
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
            clash_client or ClashClient(None),
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
    clash_client: ClashClient | None = None,
    google_publisher=None,
    accounts=None,
    roles=None,
    posts=None,
):
    bot = bot if bot is not None else MagicMock()
    clash_client = clash_client or ClashClient(None)
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
    cog.profiles = RosterProfileService(repository, clash_client)
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


class RosterAccountDirectoryTests(unittest.TestCase):
    def test_translates_link_records_without_exposing_clan_links_state(self) -> None:
        source = MagicMock()
        source.get_links_for_user.return_value = [
            {
                "player_tag": "#LIVE",
                "player_name_last_seen": "Old name",
                "last_seen_clan_code": "OLD",
            },
            {
                "player_tag": "#SAVED",
                "player_name_last_seen": "Saved name",
                "last_seen_clan_code": "BEH",
            },
        ]
        source.get_player_location.side_effect = (
            lambda tag: {
                "player_name": "Live name",
                "clan_code": "MFG",
                "townhall": 18,
            }
            if tag == "#LIVE"
            else None
        )
        directory = RosterAccountDirectory(source)

        accounts = directory.for_member(10)

        self.assertEqual(
            accounts,
            [
                LinkedAccount("#LIVE", "Live name", "MFG", 18),
                LinkedAccount("#SAVED", "Saved name", "BEH", 0),
            ],
        )
        source.get_player_location.assert_any_call("#LIVE")
        source.get_player_location.assert_any_call("#SAVED")

    def test_resolves_link_ownership_and_clan_badges(self) -> None:
        source = MagicMock()
        source.get_link_by_tag.side_effect = (
            lambda tag: {"discord_user_id": 10} if tag == "#LINKED" else None
        )
        source.get_clan_badge_url.return_value = "https://example.com/badge.png"
        directory = RosterAccountDirectory(source)

        self.assertEqual(directory.member_id_for_tag("#LINKED"), 10)
        self.assertIsNone(directory.member_id_for_tag("#MISSING"))
        self.assertEqual(
            directory.clan_badge_url("BEH"),
            "https://example.com/badge.png",
        )


class RosterRepositoryTests(unittest.TestCase):
    def test_connections_apply_roster_database_settings(self) -> None:
        path = Path(tempfile.mkdtemp()) / "rosters.sqlite3"
        repository = RosterRepository(path)

        with repository.connect() as conn:
            foreign_keys = int(conn.execute("PRAGMA foreign_keys").fetchone()[0])
            synchronous = int(conn.execute("PRAGMA synchronous").fetchone()[0])
            busy_timeout = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
            journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])

        self.assertEqual(foreign_keys, 1)
        self.assertEqual(synchronous, 1)
        self.assertEqual(busy_timeout, 30_000)
        self.assertEqual(journal_mode.casefold(), "wal")

    def test_automation_event_claim_survives_restarts_and_failed_sends_can_retry(self) -> None:
        path = Path(tempfile.mkdtemp()) / "rosters.sqlite3"
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
        self.temp_dir = tempfile.TemporaryDirectory()
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

    def test_schema_keeps_townhall_minimum_optional(self) -> None:
        with self.repository.connect() as conn:
            table_info = conn.execute("PRAGMA table_info(rosters)").fetchall()
            columns = {str(row["name"]) for row in table_info}
            column_types = {str(row["name"]): str(row["type"]) for row in table_info}
            post_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(roster_posts)").fetchall()
            }
            layout_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(roster_layouts)").fetchall()
            }
            schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])

        self.assertNotIn("category", columns)
        self.assertIn("buttons_hidden", columns)
        self.assertIn("one_off_open_ts", columns)
        self.assertIn("one_off_close_ts", columns)
        self.assertIn("min_townhall", columns)
        self.assertIn("schedule_utc_offset", columns)
        self.assertNotIn("google_sheet_id", columns)
        self.assertNotIn("channel_id", columns)
        self.assertNotIn("message_id", columns)
        self.assertEqual(
            post_columns,
            {"message_id", "roster_id", "channel_id", "created_ts"},
        )
        self.assertEqual(
            layout_columns,
            {
                "roster_id",
                "show_townhall",
                "show_discord",
                "show_clan",
                "player_width",
                "discord_width",
            },
        )
        self.assertEqual(schema_version, 5)
        self.assertIsNone(self.roster.min_townhall)
        self.assertEqual(column_types["open_day"], "TEXT")

    def test_v4_database_drops_saved_sheet_id_without_losing_rosters(self) -> None:
        path = Path(self.temp_dir.name) / "v4-rosters.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            _create_supported_schema(connection)
            connection.execute(
                """
                INSERT INTO rosters(
                    guild_id, name, google_sheet_id, created_ts, updated_ts
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (1, "Legacy roster", "saved-sheet", 1, 1),
            )
            connection.execute("PRAGMA user_version=4")
            connection.commit()

        migrated = RosterRepository(path)
        roster = migrated.list_rosters(1)[0]
        with migrated.connect() as connection:
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(rosters)"
                ).fetchall()
            }
            version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )

        self.assertEqual(roster.name, "Legacy roster")
        self.assertNotIn("google_sheet_id", columns)
        self.assertEqual(version, 5)

    def test_roster_can_track_multiple_live_posts(self) -> None:
        first = self.repository.add_post(self.roster.id, 111, 222)
        second = self.repository.add_post(self.roster.id, 333, 444)

        self.assertEqual(
            self.repository.list_posts(self.roster.id),
            [first, second],
        )

        self.repository.remove_post(first.message_id)
        self.assertEqual(self.repository.list_posts(self.roster.id), [second])

        self.repository.delete_roster(self.roster.id)
        self.assertEqual(self.repository.list_posts(self.roster.id), [])

    def test_members_for_user_are_loaded_from_active_cycles_in_one_lookup(self) -> None:
        first, _ = self.repository.start_cycle(self.roster.id, "first")
        second = self.repository.create_roster(
            guild_id=1,
            name="BEH CWL",
            clan_code="BEH",
            role_id=None,
            max_members=30,
        )
        second, _ = self.repository.start_cycle(second.id, "second")
        self.repository.add_members(
            first.id,
            first.active_cycle_id,
            10,
            [{"player_tag": "#A", "player_name": "One", "townhall": 18}],
            first.max_members,
        )
        self.repository.add_members(
            second.id,
            second.active_cycle_id,
            10,
            [{"player_tag": "#B", "player_name": "Two", "townhall": 17}],
            second.max_members,
        )
        self.repository.add_members(
            second.id,
            second.active_cycle_id,
            20,
            [{"player_tag": "#C", "player_name": "Other", "townhall": 16}],
            second.max_members,
        )

        result = self.repository.list_members_for_user((first.id, second.id), 10)

        self.assertEqual(
            {roster_id: [member.player_tag for member in members] for roster_id, members in result.items()},
            {first.id: ["#A"], second.id: ["#B"]},
        )

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

    def test_roster_layout_defaults_and_updates_are_persistent(self) -> None:
        self.assertEqual(self.repository.get_layout(self.roster.id), RosterLayout())

        updated = self.repository.update_layout(
            self.roster.id,
            show_townhall=False,
            show_discord=False,
            show_clan=True,
            player_width=18,
            discord_width=12,
        )

        self.assertEqual(
            updated,
            RosterLayout(
                show_townhall=False,
                show_discord=False,
                show_clan=True,
                player_width=18,
                discord_width=12,
            ),
        )
        restarted = RosterRepository(self.repository.path)
        self.assertEqual(restarted.get_layout(self.roster.id), updated)

    def test_deleting_a_roster_removes_its_layout(self) -> None:
        self.repository.update_layout(self.roster.id, player_width=18)

        self.repository.delete_roster(self.roster.id)

        with self.repository.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM roster_layouts WHERE roster_id = ?",
                (self.roster.id,),
            ).fetchone()
        self.assertIsNone(row)

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

    def test_roster_sorting_uses_townhall_then_heroes_then_name(self) -> None:
        roster, _ = self.repository.start_cycle(self.roster.id, "2026-07")
        self.repository.add_members(
            roster.id,
            roster.active_cycle_id,
            10,
            [
                {"player_tag": "#A", "player_name": "Zulu", "townhall": 17, "hero_sum": 400},
                {"player_tag": "#B", "player_name": "Beta", "townhall": 18, "hero_sum": 390},
                {"player_tag": "#C", "player_name": "Alpha", "townhall": 18, "hero_sum": 420},
            ],
            3,
        )

        self.assertEqual(
            [member.player_tag for member in self.repository.list_members(
                roster.id, roster.active_cycle_id
            )],
            ["#C", "#B", "#A"],
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


class RosterQueryContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_cross_feature_reads_do_not_expose_the_repository(self) -> None:
        repository = RosterRepository(
            Path(tempfile.mkdtemp()) / "rosters.sqlite3"
        )
        roster = repository.create_roster(
            guild_id=1,
            name="War Sign-up",
            clan_code="BEH",
            role_id=123,
            max_members=50,
        )
        roster, _ = repository.start_cycle(roster.id, "2026-07")
        repository.add_members(
            roster.id,
            roster.active_cycle_id,
            10,
            [{"player_tag": "#A", "player_name": "Ahmad", "townhall": 18}],
            roster.max_members,
        )
        queries = RosterQueries(repository)

        self.assertEqual(await queries.get(roster.id), roster)
        self.assertEqual(await queries.list_for_guild(1), [roster])
        self.assertEqual(
            [row.player_tag for row in await queries.members(roster)],
            ["#A"],
        )
        self.assertEqual(
            list((await queries.members_for_user((roster.id,), 10)).keys()),
            [roster.id],
        )
        self.assertTrue(await queries.role_has_signup(123, 10))
        self.assertFalse(hasattr(queries, "repository"))


class RosterScheduleTests(unittest.TestCase):
    def test_input_timezone_can_be_frozen_to_a_fixed_utc_offset(self) -> None:
        summer = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
        winter = datetime(2026, 1, 19, 12, 0, tzinfo=timezone.utc)

        self.assertEqual(fixed_utc_offset_name("Europe/Paris", summer), "UTC+02:00")
        self.assertEqual(fixed_utc_offset_name("Europe/Paris", winter), "UTC+01:00")
        self.assertEqual(
            datetime(2026, 10, 25, 12, 0, tzinfo=resolve_timezone("UTC+02:00")).utcoffset(),
            timedelta(hours=2),
        )

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

    def test_fixed_days_stop_at_28_and_month_end_is_explicit(self) -> None:
        invalid = schedule_window(
            year=2026,
            month=2,
            timezone_name="UTC",
            open_day="29",
            open_time="09:00",
            close_day="last",
            close_time="21:00",
        )
        window = schedule_window(
            year=2026,
            month=2,
            timezone_name="UTC",
            open_day="28",
            open_time="09:00",
            close_day="last",
            close_time="21:00",
        )

        self.assertIsNone(invalid)
        self.assertIsNotNone(window)
        self.assertEqual(window.opens_at, datetime(2026, 2, 28, 9, 0, tzinfo=timezone.utc))
        self.assertEqual(window.closes_at, datetime(2026, 2, 28, 21, 0, tzinfo=timezone.utc))

    def test_both_days_support_month_end_offsets(self) -> None:
        window = schedule_window(
            year=2026,
            month=4,
            timezone_name="UTC",
            open_day="last-2",
            open_time="11:00",
            close_day="last-1",
            close_time="20:00",
        )

        self.assertIsNotNone(window)
        self.assertEqual(window.opens_at, datetime(2026, 4, 28, 11, 0, tzinfo=timezone.utc))
        self.assertEqual(window.closes_at, datetime(2026, 4, 29, 20, 0, tzinfo=timezone.utc))

    def test_close_rolls_forward_when_its_rule_is_not_after_opening(self) -> None:
        window = schedule_window(
            year=2026,
            month=7,
            timezone_name="UTC",
            open_day="last-1",
            open_time="11:00",
            close_day="2",
            close_time="20:00",
        )

        self.assertIsNotNone(window)
        self.assertEqual(window.opens_at, datetime(2026, 7, 30, 11, 0, tzinfo=timezone.utc))
        self.assertEqual(window.closes_at, datetime(2026, 8, 2, 20, 0, tzinfo=timezone.utc))

    def test_same_day_earlier_close_time_rolls_to_the_next_month(self) -> None:
        window = schedule_window(
            year=2026,
            month=7,
            timezone_name="UTC",
            open_day="last",
            open_time="20:00",
            close_day="last",
            close_time="19:00",
        )

        self.assertIsNotNone(window)
        self.assertEqual(window.opens_at, datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc))
        self.assertEqual(window.closes_at, datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc))

    def test_nonexistent_dst_time_moves_to_the_next_real_local_time(self) -> None:
        window = schedule_window(
            year=2026,
            month=3,
            timezone_name="Europe/Paris",
            open_day="last-2",
            open_time="02:30",
            close_day="last-1",
            close_time="02:30",
        )

        self.assertIsNotNone(window)
        self.assertEqual(window.opens_at, datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc))

    def test_ambiguous_dst_time_uses_the_first_occurrence(self) -> None:
        window = schedule_window(
            year=2026,
            month=10,
            timezone_name="Europe/Paris",
            open_day="25",
            open_time="02:30",
            close_day="26",
            close_time="02:30",
        )

        self.assertIsNotNone(window)
        self.assertEqual(window.opens_at, datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc))

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

    def test_monthly_window_uses_timezone_and_cross_month_close(self) -> None:
        window = schedule_window(
            year=2026,
            month=3,
            timezone_name="Europe/Paris",
            open_day=28,
            open_time="11:00",
            close_day="2",
            close_time="20:00",
        )
        self.assertIsNotNone(window)
        self.assertEqual(window.cycle_key, "2026-03")
        self.assertEqual(window.opens_at, datetime(2026, 3, 28, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(window.closes_at, datetime(2026, 4, 2, 18, 0, tzinfo=timezone.utc))

    def test_monthly_window_supports_days_before_month_end(self) -> None:
        window = schedule_window(
            year=2026,
            month=2,
            timezone_name="Asia/Beirut",
            open_day=18,
            open_time="09:00",
            close_day="last-2",
            close_time="21:00",
        )

        self.assertIsNotNone(window)
        self.assertEqual(window.closes_at, datetime(2026, 2, 26, 19, 0, tzinfo=timezone.utc))

    def test_due_window_supports_restart_catchup(self) -> None:
        repository = RosterRepository(Path(tempfile.mkdtemp()) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL",
            clan_code="FAMILY",
            role_id=123,
            max_members=150,
        )
        roster = repository.configure_schedule(
            roster.id,
            enabled=True,
            timezone_name="UTC",
            open_day="18",
            open_time="11:00",
            close_day="last",
            close_time="20:00",
            reset_on_open=True,
        )
        window = due_window(roster, datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(window.cycle_key, "2026-07")

    def test_next_window_returns_the_next_opening_after_a_cycle_closes(self) -> None:
        repository = RosterRepository(Path(tempfile.mkdtemp()) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL",
            clan_code="FAMILY",
            role_id=123,
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
            reset_on_open=True,
        )

        window = next_window(
            roster,
            datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(window.cycle_key, "2026-08")
        self.assertEqual(window.opens_at, datetime(2026, 8, 29, 11, 0, tzinfo=timezone.utc))


class RosterRenderingTests(unittest.TestCase):
    def test_public_roster_uses_discord_username_instead_of_server_nickname(self) -> None:
        repository = RosterRepository(Path(tempfile.mkdtemp()) / "rosters.sqlite3")
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
        discord_member = MagicMock()
        discord_member.name = "ahmad_user"
        discord_member.display_name = "Server Nick"
        guild = MagicMock()
        guild.icon = None
        guild.get_member.return_value = discord_member
        cog = object.__new__(Rosters)
        bot = MagicMock()
        bot.get_guild.return_value = guild
        emojis = MagicMock()
        emojis.get = AsyncMock(
            return_value=TownHallEmojiSet(header=None, levels={})
        )
        accounts = MagicMock()
        accounts.clan_badge_url.return_value = None
        posts = RosterPostService(
            bot,
            repository,
            ClashClient(None),
            accounts,
            cog,
            emojis,
        )

        embeds, _, _ = asyncio.run(posts.render(roster))

        self.assertIn("ahmad_user", embeds[0].description)
        self.assertNotIn("Server Nick", embeds[0].description)

    def test_clan_layout_status_and_second_page_output(self) -> None:
        repository = RosterRepository(Path(tempfile.mkdtemp()) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=123,
            max_members=500,
        )
        roster = replace(roster, status="open")
        members = [
            RosterMember(
                player_tag=f"#{index}",
                discord_user_id=index,
                player_name=f"Player {index}",
                clan_code="BEH",
                townhall=18,
                signed_up_ts=index,
                hero_sum=400 + index,
            )
            for index in range(1, 52)
        ]
        closes_at = datetime(2026, 7, 30, 18, 0, tzinfo=timezone.utc)
        embeds = build_roster_embeds(
            roster,
            members,
            {index: f"Member {index}" for index in range(1, 52)},
            closes_at,
            page=1,
        )

        self.assertEqual(len(embeds), 1)
        self.assertIn("`TH PLAYER", embeds[0].description)
        self.assertNotIn("#   TH", embeds[0].description)
        self.assertIn("Player 51", embeds[0].description)
        self.assertNotIn("Player 1 `", embeds[0].description)
        self.assertIn(" BEH`", embeds[0].description)
        self.assertNotIn("451", embeds[0].description)
        self.assertNotIn("HERO", embeds[0].description)
        self.assertTrue(
            all(line.count("`") == 2 for line in embeds[0].description.splitlines())
        )
        self.assertNotIn("<:", embeds[0].description)
        self.assertIsNone(embeds[0].footer.text)
        self.assertEqual(
            embeds[0].fields[0].value,
            "Role <@&123>\nTotal 51/500\nSignup closes on <t:1785434400>",
        )

    def test_empty_roster_has_no_padded_empty_state(self) -> None:
        repository = RosterRepository(Path(tempfile.mkdtemp()) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=123,
            max_members=500,
        )
        embed = build_roster_embeds(
            roster,
            [],
            family_icon_url="https://example.com/server.png",
        )[0]

        self.assertEqual(
            embed.description,
            "`TH PLAYER         DISCORD    CLAN`",
        )
        self.assertNotIn("No signups", embed.description)
        self.assertEqual(embed.author.name, "Brown Elbow Clan Family")
        self.assertEqual(embed.author.icon_url, "https://example.com/server.png")
        self.assertIsNone(embed.footer.text)

    def test_layout_can_hide_optional_columns_and_resize_player_names(self) -> None:
        repository = RosterRepository(Path(tempfile.mkdtemp()) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="BEH War Signup",
            clan_code="BEH",
            role_id=None,
            max_members=50,
        )
        member = RosterMember(
            player_tag="#A",
            discord_user_id=10,
            player_name="A Very Long Player Name",
            clan_code="BEH",
            townhall=18,
            signed_up_ts=1,
            hero_sum=450,
        )

        embed = build_roster_embeds(
            roster,
            [member],
            {10: "discord_name"},
            layout=RosterLayout(
                show_townhall=False,
                show_discord=False,
                show_clan=False,
                player_width=18,
                discord_width=10,
            ),
        )[0]

        lines = embed.description.splitlines()
        self.assertEqual(lines[0], "`PLAYER            `")
        self.assertNotIn("TH", embed.description)
        self.assertNotIn("DISCORD", embed.description)
        self.assertNotIn("CLAN", embed.description)
        self.assertNotIn("discord_name", embed.description)
        self.assertIn("A Very Long Playe…", lines[1])

    def test_layout_uses_configured_player_and_discord_widths(self) -> None:
        repository = RosterRepository(Path(tempfile.mkdtemp()) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="BEH War Signup",
            clan_code="BEH",
            role_id=None,
            max_members=50,
        )
        member = RosterMember("#A", 10, "LongPlayer", "BEH", 18, 1, 450)

        embed = build_roster_embeds(
            roster,
            [member],
            {10: "long_discord"},
            layout=RosterLayout(
                player_width=8,
                discord_width=7,
            ),
        )[0]

        self.assertEqual(embed.description.splitlines()[0], "`TH PLAYER   DISCORD CLAN`")
        self.assertIn("LongPla…", embed.description)
        self.assertIn("long_d…", embed.description)
        self.assertEqual(
            len(embed.description.splitlines()[0]),
            len(embed.description.splitlines()[1]),
        )

    def test_widest_layout_stays_within_discord_embed_limits(self) -> None:
        repository = RosterRepository(Path(tempfile.mkdtemp()) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="BEH War Signup",
            clan_code="BEH",
            role_id=None,
            max_members=500,
        )
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

    def test_townhall_emojis_replace_numbers_without_breaking_pagination(self) -> None:
        repository = RosterRepository(Path(tempfile.mkdtemp()) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="BEH War Signup",
            clan_code="BEH",
            role_id=None,
            max_members=500,
        )
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
        emojis = TownHallEmojiSet(
            header="<:town_hall:1234567890123456789>",
            levels={18: "<:th18:1234567890123456790>"},
        )
        layout = RosterLayout(player_width=24, discord_width=20)

        first = build_roster_embeds(
            roster,
            members,
            display_names,
            layout=layout,
            townhall_emojis=emojis,
            page=0,
        )[0]
        second = build_roster_embeds(
            roster,
            members,
            display_names,
            layout=layout,
            townhall_emojis=emojis,
            page=1,
        )[0]

        self.assertEqual(roster_rows_per_page(members, layout, emojis), 40)
        self.assertTrue(
            first.description.startswith(
                "<:town_hall:1234567890123456789> `PLAYER"
            )
        )
        self.assertIn(
            "<:th18:1234567890123456790> `Long Player",
            first.description,
        )
        self.assertNotIn("`TH PLAYER", first.description)
        self.assertEqual(len(first.description.splitlines()), 41)
        self.assertEqual(len(second.description.splitlines()), 11)
        self.assertLessEqual(len(first.description), 4096)
        self.assertLessEqual(len(first), 6000)

    def test_missing_townhall_emoji_keeps_the_numeric_column(self) -> None:
        repository = RosterRepository(Path(tempfile.mkdtemp()) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="BEH War Signup",
            clan_code="BEH",
            role_id=None,
            max_members=500,
        )
        members = [
            RosterMember("#A", 10, "Ahmad", "BEH", 18, 1, 450),
            RosterMember("#B", 11, "Luna", "BEH", 17, 2, 430),
        ]
        emojis = TownHallEmojiSet(
            header="<:town_hall:100>",
            levels={18: "<:th18:118>"},
        )

        embed = build_roster_embeds(
            roster,
            members,
            townhall_emojis=emojis,
        )[0]

        self.assertTrue(embed.description.startswith("`TH PLAYER"))
        self.assertNotIn("<:", embed.description)

    def test_clan_roster_links_its_live_badge_and_clan_page(self) -> None:
        repository = RosterRepository(Path(tempfile.mkdtemp()) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="Test Roster",
            clan_code="BEH",
            role_id=None,
            max_members=500,
        )

        embed = build_roster_embeds(
            roster,
            [],
            clan_icon_url="https://example.com/beh.png",
        )[0]

        self.assertEqual(embed.title, "Test Roster")
        self.assertEqual(embed.author.name, "Hellbow • #2Y2PJCVGU")
        self.assertEqual(embed.author.url, "http://cprk.us/c/2Y2PJCVGU")
        self.assertEqual(embed.author.icon_url, "https://example.com/beh.png")

    def test_future_one_off_timing_is_shown_in_the_embed(self) -> None:
        repository = RosterRepository(Path(tempfile.mkdtemp()) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=123,
            max_members=500,
        )
        opens_at = datetime(2026, 7, 20, 6, 0, tzinfo=timezone.utc)

        embed = build_roster_embeds(roster, [], opens_at=opens_at)[0]

        self.assertEqual(
            embed.fields[0].value,
            "Role <@&123>\nTotal 0/500\nSignup opens on <t:1784527200>",
        )

    def test_hidden_signup_buttons_hide_signup_state_and_timing(self) -> None:
        repository = RosterRepository(Path(tempfile.mkdtemp()) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="BEH CWL",
            clan_code="BEH",
            role_id=None,
            max_members=30,
        )
        opens_at = datetime(2026, 7, 29, 6, 0, tzinfo=timezone.utc)

        closed_embed = build_roster_embeds(
            replace(roster, buttons_hidden=True),
            [],
            opens_at=opens_at,
        )[0]
        open_embed = build_roster_embeds(
            replace(roster, buttons_hidden=True, status="open"),
            [],
            closes_at=datetime(2026, 8, 11, 18, 0, tzinfo=timezone.utc),
        )[0]

        self.assertEqual(closed_embed.fields[0].value, "Total 0/30")
        self.assertEqual(open_embed.fields[0].value, "Total 0/30")
        self.assertNotIn("Signup", closed_embed.fields[0].value)
        self.assertNotIn("Signup", open_embed.fields[0].value)

    def test_embed_shows_only_a_configured_townhall_minimum(self) -> None:
        repository = RosterRepository(Path(tempfile.mkdtemp()) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )

        unrestricted = build_roster_embeds(roster, [])[0]
        restricted = build_roster_embeds(replace(roster, min_townhall=8), [])[0]

        self.assertIn("Total 0/500", unrestricted.fields[0].value)
        self.assertNotIn("Min. TH", unrestricted.fields[0].value)
        self.assertIn("Total 0/500 | Min. TH8", restricted.fields[0].value)

    def test_large_roster_stays_within_discord_embed_limits(self) -> None:
        repository = RosterRepository(Path(tempfile.mkdtemp()) / "rosters.sqlite3")
        roster = replace(
            repository.create_roster(
                guild_id=1,
                name="CWL Sign-up",
                clan_code="BEH",
                role_id=123,
                max_members=500,
            ),
            status="open",
        )
        members = [
            RosterMember(
                player_tag=f"#{index}",
                discord_user_id=index,
                player_name=f"Player {index}",
                clan_code="BEH",
                townhall=18,
                signed_up_ts=index,
                hero_sum=420,
            )
            for index in range(500)
        ]
        first_page = build_roster_embeds(roster, members, page=0)
        last_page = build_roster_embeds(roster, members, page=9)

        self.assertLessEqual(sum(len(embed) for embed in first_page), 6000)
        self.assertLessEqual(sum(len(embed) for embed in last_page), 6000)
        self.assertIn("Total 500/500", first_page[0].fields[0].value)
        self.assertIn("Player 0", first_page[0].description)
        self.assertIn("Player 499", last_page[0].description)
        self.assertNotIn("Showing", first_page[0].fields[0].value)


class RosterEmojiTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_emojis_are_discovered_by_name_and_cached(self) -> None:
        client = MagicMock()
        client.fetch_application_emojis = AsyncMock(
            return_value=[
                discord.PartialEmoji(name="town_hall", id=100),
                *[
                    discord.PartialEmoji(name=f"th{level}", id=100 + level)
                    for level in range(1, 19)
                ],
            ]
        )
        provider = TownHallEmojiProvider(client)

        loaded = await provider.get()
        cached = await provider.get()

        self.assertTrue(loaded.is_complete)
        self.assertEqual(loaded.header, "<:town_hall:100>")
        self.assertEqual(loaded.levels[18], "<:th18:118>")
        self.assertIs(cached, loaded)
        client.fetch_application_emojis.assert_awaited_once_with()

    async def test_ready_refreshes_existing_posts_once_emojis_are_available(self) -> None:
        complete = TownHallEmojiSet(
            header="<:town_hall:100>",
            levels={level: f"<:th{level}:{100 + level}>" for level in range(1, 19)},
        )
        roster = MagicMock(id=7)
        cog = object.__new__(Rosters)
        repository = MagicMock()
        repository.list_posts.return_value = [MagicMock(roster_id=7)]
        repository.get_roster.return_value = roster
        emojis = MagicMock()
        emojis.get = AsyncMock(return_value=complete)
        accounts = MagicMock()
        cog.posts = RosterPostService(
            MagicMock(),
            repository,
            ClashClient(None),
            accounts,
            cog,
            emojis,
        )
        cog.posts.refresh = AsyncMock()

        await cog.on_ready()
        await cog.on_ready()

        emojis.get.assert_awaited_once_with()
        cog.posts.refresh.assert_awaited_once_with(roster)


class RosterDeletionTests(unittest.IsolatedAsyncioTestCase):
    def _roster_with_signup(self) -> tuple[RosterRepository, Roster]:
        temp_dir = tempfile.TemporaryDirectory()
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

    async def test_role_cleanup_failure_preserves_roster_and_signups(self) -> None:
        repository, roster = self._roster_with_signup()
        roles = MagicMock()
        roles.sync = AsyncMock(return_value=False)
        posts = MagicMock()
        posts.disable_all = AsyncMock(return_value=())
        service = self._service(repository, roles, posts)

        with self.assertRaises(RosterDeleteCleanupError) as raised:
            await service.delete(roster)

        self.assertEqual(raised.exception.member_ids, (42,))
        self.assertIsNotNone(repository.get_roster(roster.id))
        self.assertEqual(len(repository.list_members(roster.id, roster.active_cycle_id)), 1)
        posts.disable_all.assert_not_awaited()

    async def test_post_cleanup_failure_preserves_roster_and_signups(self) -> None:
        repository, roster = self._roster_with_signup()
        repository.add_post(roster.id, 111, 222)
        roles = MagicMock()
        roles.sync = AsyncMock(return_value=True)
        posts = MagicMock()
        posts.disable_all = AsyncMock(return_value=(222,))
        service = self._service(repository, roles, posts)

        with self.assertRaises(RosterDeleteCleanupError) as raised:
            await service.delete(roster)

        self.assertEqual(raised.exception.message_ids, (222,))
        self.assertIsNotNone(repository.get_roster(roster.id))
        self.assertEqual(len(repository.list_members(roster.id, roster.active_cycle_id)), 1)

    async def test_post_cleanup_keeps_unmodified_posts_as_failures(self) -> None:
        repository, roster = self._roster_with_signup()
        repository.add_post(roster.id, 111, 222)
        response = MagicMock(status=500, reason="Internal Server Error")
        message = MagicMock()
        message.edit = AsyncMock(
            side_effect=discord.HTTPException(response, "temporary failure")
        )
        channel = MagicMock()
        channel.fetch_message = AsyncMock(return_value=message)
        bot = MagicMock()
        bot.get_channel.return_value = channel
        posts = RosterPostService(
            bot,
            repository,
            ClashClient(None),
            MagicMock(),
            MagicMock(),
        )

        failed = await posts.disable_all(roster)

        self.assertEqual(failed, (222,))
        self.assertEqual(len(repository.list_posts(roster.id)), 1)

    async def test_post_cleanup_accepts_messages_that_are_already_gone(self) -> None:
        repository, roster = self._roster_with_signup()
        repository.add_post(roster.id, 111, 222)
        response = MagicMock(status=404, reason="Not Found")
        channel = MagicMock()
        channel.fetch_message = AsyncMock(
            side_effect=discord.NotFound(response, "missing")
        )
        bot = MagicMock()
        bot.get_channel.return_value = channel
        posts = RosterPostService(
            bot,
            repository,
            ClashClient(None),
            MagicMock(),
            MagicMock(),
        )

        failed = await posts.disable_all(roster)

        self.assertEqual(failed, ())
        self.assertEqual(repository.list_posts(roster.id), [])


class RosterComponentTests(unittest.IsolatedAsyncioTestCase):
    class _Cog:
        pass

    async def test_public_buttons_only_use_refresh_and_settings_icons(self) -> None:
        view = RosterMessageView(self._Cog(), 7)
        signup = next(item for item in view.children if item.label == "Signup")
        opt_out = next(item for item in view.children if item.label == "Opt-out")
        refresh = view.children[0]

        self.assertFalse(bool(signup.emoji))
        self.assertFalse(bool(opt_out.emoji))
        self.assertEqual(str(refresh.emoji), "🔁")
        self.assertEqual(sum(bool(item.emoji) for item in view.children), 2)

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

    async def test_roster_create_confirmation_only_states_what_changed(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        cog = object.__new__(Rosters)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        _wire_roster_service(cog, repository)
        cog._require_lead = AsyncMock(return_value=True)
        interaction = MagicMock()
        interaction.guild_id = 1
        interaction.response.send_message = AsyncMock()
        clan = app_commands.Choice(name="BEH - Hellbow", value="BEH")

        await cog.roster_create(interaction, "Test Roster", clan)

        interaction.response.send_message.assert_awaited_once_with(
            "Created **Test Roster**.",
            ephemeral=True,
        )

    async def test_roster_clone_applies_selected_overrides(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
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

    async def test_roster_list_omits_timing_when_none_is_configured(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        cog = object.__new__(Rosters)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        repository.create_roster(
            guild_id=1,
            name="Test Roster",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )
        _wire_roster_service(cog, repository)
        cog._require_lead = AsyncMock(return_value=True)
        interaction = MagicMock()
        interaction.guild_id = 1
        interaction.response.send_message = AsyncMock()

        await cog.roster_list(interaction)

        interaction.response.send_message.assert_awaited_once_with(
            "- **Test Roster** — Closed — 500 accounts max",
            ephemeral=True,
        )

    async def test_roster_post_uses_the_command_response_as_the_live_roster(self) -> None:
        cog = object.__new__(Rosters)
        cog._locks = {}
        cog._require_lead = AsyncMock(return_value=True)
        roster = MagicMock()
        roster.id = 7
        roster.name = "Test Roster"
        cog._resolve_roster = AsyncMock(return_value=roster)
        cog.service = MagicMock()
        cog.service.get = AsyncMock(return_value=roster)
        cog.service.open = AsyncMock(return_value=roster)
        cog.posts = MagicMock()
        cog.posts.post_interaction_response = AsyncMock()
        interaction = MagicMock()
        interaction.channel = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        await cog.roster_post(interaction, "7")

        interaction.response.defer.assert_awaited_once_with(thinking=True)
        cog.service.get.assert_awaited_once_with(roster.id)
        cog.service.open.assert_awaited_once_with(roster)
        cog.posts.post_interaction_response.assert_awaited_once_with(
            roster,
            interaction,
        )
        interaction.followup.send.assert_not_awaited()

    async def test_roster_delete_reports_incomplete_cleanup_without_success(self) -> None:
        cog = object.__new__(Rosters)
        cog._locks = {}
        roster = MagicMock()
        roster.id = 7
        roster.name = "CWL Sign-up"
        cog.service = MagicMock()
        cog.service.get = AsyncMock(return_value=roster)
        cog.service.delete = AsyncMock(
            side_effect=RosterDeleteCleanupError(message_ids=(222,))
        )
        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.edit_original_response = AsyncMock()

        await cog.confirm_delete(interaction, roster.id)

        interaction.response.defer.assert_awaited_once_with()
        interaction.edit_original_response.assert_awaited_once_with(
            content=(
                "**CWL Sign-up** was not deleted because one or more signup "
                "roles or roster posts could not be removed. Try again."
            ),
            view=None,
        )

    async def test_roster_delete_reports_success_after_cleanup(self) -> None:
        cog = object.__new__(Rosters)
        cog._locks = {}
        roster = MagicMock()
        roster.id = 7
        roster.name = "CWL Sign-up"
        cog.service = MagicMock()
        cog.service.get = AsyncMock(return_value=roster)
        cog.service.delete = AsyncMock()
        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.edit_original_response = AsyncMock()

        await cog.confirm_delete(interaction, roster.id)

        cog.service.delete.assert_awaited_once_with(roster)
        interaction.edit_original_response.assert_awaited_once_with(
            content="Deleted **CWL Sign-up**.",
            view=None,
        )

    async def test_interaction_roster_post_is_registered_for_live_updates(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
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
        bot = MagicMock()
        embeds = [MagicMock()]
        view = MagicMock()
        accounts = MagicMock()
        posts = RosterPostService(
            bot,
            repository,
            ClashClient(None),
            accounts,
            cog,
        )
        posts.render = AsyncMock(return_value=(embeds, 0, 1))
        posts.message_view = MagicMock(return_value=view)
        message = MagicMock(id=222)
        message.channel.id = 111
        interaction = MagicMock()
        interaction.edit_original_response = AsyncMock(return_value=message)

        posted = await posts.post_interaction_response(roster, interaction)

        self.assertIs(posted, message)
        interaction.edit_original_response.assert_awaited_once_with(
            content=None,
            embeds=embeds,
            view=view,
        )
        self.assertEqual(
            [(post.channel_id, post.message_id) for post in repository.list_posts(roster.id)],
            [(111, 222)],
        )
        bot.add_view.assert_called_once_with(view, message_id=222)

    async def test_refresh_updates_every_registered_roster_post(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
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
            MagicMock(),
            repository,
            ClashClient(None),
            MagicMock(),
            cog,
        )
        posts.fetch = AsyncMock(side_effect=[first_message, second_message])
        posts.render = AsyncMock(return_value=([MagicMock()], 0, 1))
        posts.message_view = MagicMock(return_value=MagicMock())

        await posts.refresh(roster)

        first_message.edit.assert_awaited_once()
        second_message.edit.assert_awaited_once()

    async def test_deleted_roster_message_is_unregistered(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
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
        temp_dir = tempfile.TemporaryDirectory()
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
        bot = MagicMock()
        bot.get_channel.return_value = object()
        posts = RosterPostService(
            bot,
            repository,
            ClashClient(None),
            MagicMock(),
            cog,
        )

        message = await posts.fetch(post)

        self.assertIsNone(message)
        self.assertEqual(repository.list_posts(roster.id), [])

    async def test_large_roster_adds_page_controls_without_hiding_actions(self) -> None:
        view = RosterMessageView(self._Cog(), 7, page=1, page_count=3)
        labels = [getattr(item, "label", None) for item in view.children]

        self.assertIn("Signup", labels)
        self.assertIn("Opt-out", labels)
        self.assertIn(PREV_PAGE_LABEL, labels)
        self.assertIn("Page 2/3", labels)
        self.assertIn(NEXT_PAGE_LABEL, labels)

    async def test_roster_pagination_uses_shared_adaptive_jump_controls(self) -> None:
        view = RosterMessageView(
            self._Cog(),
            7,
            page=1,
            page_count=ADAPTIVE_JUMP_THRESHOLD + 1,
        )
        labels = [item.label for item in view.children]

        self.assertIn(FIRST_PAGE_LABEL, labels)
        self.assertIn(PREV_PAGE_LABEL, labels)
        self.assertIn(NEXT_PAGE_LABEL, labels)
        self.assertIn(LAST_PAGE_LABEL, labels)

    async def test_removal_pagination_uses_shared_navigation(self) -> None:
        members = [
            RosterMember(f"#{index}", index, f"Player {index}", "BEH", 18, index, 400)
            for index in range(76)
        ]
        view = RosterRemovalView(self._Cog(), 7, members, {}, page=1)
        labels = [getattr(item, "label", None) for item in view.children]

        self.assertIn(FIRST_PAGE_LABEL, labels)
        self.assertIn(PREV_PAGE_LABEL, labels)
        self.assertIn("Page 2/4", labels)
        self.assertIn(NEXT_PAGE_LABEL, labels)
        self.assertIn(LAST_PAGE_LABEL, labels)

    async def test_persistent_roster_pagination_uses_repository_page_count(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
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
        bot = MagicMock()
        posts = RosterPostService(
            bot,
            repository,
            ClashClient(None),
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

    async def test_hidden_signup_buttons_leave_refresh_settings_and_pages(self) -> None:
        view = RosterMessageView(
            self._Cog(),
            7,
            buttons_hidden=True,
            page=0,
            page_count=2,
        )
        labels = [item.label for item in view.children]

        self.assertNotIn("Signup", labels)
        self.assertNotIn("Opt-out", labels)
        self.assertIn(NEXT_PAGE_LABEL, labels)
        self.assertEqual(sum(bool(item.emoji) for item in view.children), 2)

    async def test_settings_menu_has_no_option_emojis_and_one_state_action(self) -> None:
        view = RosterSettingsView(
            self._Cog(),
            7,
            is_open=True,
            buttons_hidden=False,
        )
        select = view.children[0]

        self.assertTrue(all(option.emoji is None for option in select.options))
        self.assertIn("Close roster", [option.label for option in select.options])
        self.assertNotIn("Open roster", [option.label for option in select.options])
        self.assertIn("Hide buttons", [option.label for option in select.options])
        self.assertIn("Add accounts", [option.label for option in select.options])
        self.assertIn("Remove accounts", [option.label for option in select.options])
        self.assertIn("Layout", [option.label for option in select.options])
        self.assertNotIn("Clash accounts", " ".join(option.label for option in select.options))

    async def test_layout_editor_preserves_current_columns_and_width_action(self) -> None:
        layout = RosterLayout(
            show_townhall=True,
            show_discord=False,
            show_clan=True,
            player_width=18,
            discord_width=12,
        )
        view = RosterLayoutView(self._Cog(), 7, layout)
        select = view.children[0]

        defaults = {option.value for option in select.options if option.default}
        self.assertEqual(defaults, {"townhall", "clan"})
        self.assertEqual(select.min_values, 0)
        labels = [getattr(item, "label", None) for item in view.children]
        self.assertIn("Edit name lengths", labels)
        self.assertIn("Back", labels)

        modal = RosterColumnWidthsModal(self._Cog(), 7, layout)
        self.assertEqual(modal.title, "Set roster name lengths")
        self.assertEqual(
            [item.to_component_dict()["label"] for item in modal.children],
            [
                "Player names (6–24 characters)",
                "Discord usernames (7–20 characters)",
            ],
        )

    async def test_layout_changes_persist_and_refresh_every_post(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
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

    async def test_roster_export_replaces_the_selector_with_progress(self) -> None:
        roster = MagicMock()
        roster.id = 7
        roster.name = "CWL Sign-up"
        cog = object.__new__(Rosters)
        cog._locks = {}
        cog.service = MagicMock()
        cog.service.get = AsyncMock(return_value=roster)
        cog.is_lead = MagicMock(return_value=True)
        cog.publisher = MagicMock()
        report = SimpleNamespace(
            google_link=(
                "https://docs.google.com/spreadsheets/d/sheet-id/edit"
            ),
            google_warning=None,
        )
        cog.publisher.export = AsyncMock(return_value=(report, None))
        cog.publisher.discard = AsyncMock()
        interaction = MagicMock()
        interaction.response.edit_message = AsyncMock()
        interaction.edit_original_response = AsyncMock()

        await cog.handle_management_action(interaction, roster.id, "export")

        interaction.response.edit_message.assert_awaited_once()
        progress = interaction.response.edit_message.await_args.kwargs["view"]
        self.assertIsInstance(progress, RosterProgressView)
        self.assertEqual(progress.children[0].label, "Exporting signups…")
        self.assertTrue(progress.children[0].disabled)
        cog.publisher.export.assert_awaited_once_with(roster)
        final = interaction.edit_original_response.await_args.kwargs
        self.assertEqual(final["content"], "Exported **CWL Sign-up**.")
        self.assertEqual(
            [item.label for item in final["view"].children],
            ["Google Sheet", "Download"],
        )
        cog.publisher.discard.assert_awaited_once_with(report)

    async def test_roster_export_falls_back_to_discord_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
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
        with tempfile.TemporaryDirectory() as directory:
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

    async def test_clear_confirmation_shows_progress_and_replaces_it(self) -> None:
        cog = object.__new__(Rosters)
        cog.membership = MagicMock()
        cog.membership.clear = AsyncMock(
            return_value=MembershipResult(
                "Cleared current signups for 2 members from **CWL Sign-up**."
            )
        )
        interaction = MagicMock()
        interaction.response.edit_message = AsyncMock()
        interaction.edit_original_response = AsyncMock()

        await cog.confirm_clear(interaction, 7)

        progress = interaction.response.edit_message.await_args.kwargs["view"]
        self.assertIsInstance(progress, RosterProgressView)
        self.assertEqual(progress.children[0].label, "Clearing signups…")
        self.assertTrue(progress.children[0].disabled)
        cog.membership.clear.assert_awaited_once_with(7)
        interaction.edit_original_response.assert_awaited_once_with(
            content="Cleared current signups for 2 members from **CWL Sign-up**.",
            view=None,
        )

    async def test_clear_workflow_removes_signups_roles_and_refreshes_posts(self) -> None:
        repository = RosterRepository(Path(tempfile.mkdtemp()) / "rosters.sqlite3")
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

    async def test_roster_removal_selector_lists_signed_up_players_directly(self) -> None:
        members = [
            RosterMember("#A", 10, "Ahmad", "BEH", 18, 1, 420),
            RosterMember("#B", 11, "Luna", "BE4", 17, 2, 390),
        ]
        view = RosterRemovalView(
            self._Cog(),
            7,
            members,
            {10: "ahmad", 11: "luna"},
        )
        select = next(
            item
            for item in view.children
            if getattr(item, "placeholder", None) == "Select accounts to remove"
        )

        self.assertEqual(select.placeholder, "Select accounts to remove")
        self.assertEqual([option.value for option in select.options], ["#A", "#B"])
        self.assertIn("ahmad", select.options[0].description)
        remove = next(
            item for item in view.children if getattr(item, "label", None) == "Remove"
        )
        self.assertTrue(remove.disabled)

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

    async def test_roster_removal_can_filter_by_discord_member(self) -> None:
        members = [
            RosterMember("#A", 10, "Ahmad", "BEH", 18, 1, 420),
            RosterMember("#B", 11, "Luna", "BE4", 17, 2, 390),
        ]
        view = RosterRemovalView(self._Cog(), 7, members, {})
        interaction = MagicMock()
        interaction.response.edit_message = AsyncMock()

        await view.filter_member(interaction, 10)

        filtered = interaction.response.edit_message.await_args.kwargs["view"]
        self.assertEqual([member.player_tag for member in filtered.filtered_members], ["#A"])
        self.assertIsNone(interaction.response.edit_message.await_args.kwargs["content"])

    async def test_empty_member_filter_keeps_all_roster_players_available(self) -> None:
        members = [RosterMember("#A", 10, "Ahmad", "BEH", 18, 1, 420)]
        view = RosterRemovalView(self._Cog(), 7, members, {})
        interaction = MagicMock()
        interaction.response.edit_message = AsyncMock()

        await view.filter_member(interaction, 99)

        restored = interaction.response.edit_message.await_args.kwargs["view"]
        self.assertEqual(
            interaction.response.edit_message.await_args.kwargs["content"],
            "That member has no signups.",
        )
        self.assertEqual(
            [member.player_tag for member in restored.filtered_members],
            ["#A"],
        )
        self.assertTrue(
            any(
                getattr(item, "placeholder", None) == "Select accounts to remove"
                for item in restored.children
            )
        )

    async def test_account_selector_uses_tag_and_clan_details(self) -> None:
        account = LinkedAccount(
            player_tag="#ABC",
            player_name="Ahmad",
            clan_code="BEH",
            townhall=18,
            hero_sum=420,
            hero_levels=(("BK", 100), ("AQ", 100)),
        )
        view = AccountPickerView(
            self._Cog(),
            7,
            member_id=10,
            accounts=[account],
            mode="signup",
            lead_override=False,
        )
        select = view.children[0]

        self.assertEqual(select.placeholder, "Select accounts")
        self.assertEqual(select.options[0].label, "Ahmad (#ABC)")
        self.assertEqual(select.options[0].description, "TH18 • BEH")
        self.assertIsNone(select.options[0].emoji)

    async def test_account_selector_sorts_by_townhall_heroes_and_name(self) -> None:
        accounts = [
            LinkedAccount("#A", "Zulu", "BEH", 17, 400),
            LinkedAccount("#B", "Beta", "BEH", 18, 390),
            LinkedAccount("#C", "Alpha", "BEH", 18, 420),
        ]
        view = AccountPickerView(
            self._Cog(),
            7,
            member_id=10,
            accounts=accounts,
            mode="signup",
            lead_override=False,
        )

        self.assertEqual(
            [option.value for option in view.children[0].options],
            ["#C", "#B", "#A"],
        )

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

    async def test_staff_addition_offers_member_and_bulk_paths(self) -> None:
        view = RosterTargetMemberView(self._Cog(), 7, mode="add")

        self.assertTrue(
            any(getattr(item, "placeholder", None) == "Select a member" for item in view.children)
        )
        self.assertTrue(
            any(getattr(item, "label", None) == "Bulk add" for item in view.children)
        )

    async def test_roster_removal_has_an_explicit_deselect_action(self) -> None:
        members = [RosterMember("#A", 10, "Ahmad", "BEH", 18, 1, 420)]
        view = RosterRemovalView(
            self._Cog(),
            7,
            members,
            {10: "ahmad"},
            selected_tags={"#A"},
        )
        interaction = MagicMock()
        interaction.response.edit_message = AsyncMock()

        await view.deselect(interaction)

        next_view = interaction.response.edit_message.await_args.kwargs["view"]
        self.assertFalse(next_view.selected_tags)
        deselect = next(
            item
            for item in next_view.children
            if getattr(item, "label", None) == "Deselect"
        )
        self.assertTrue(deselect.disabled)

    async def test_add_accounts_opens_one_clean_management_surface(self) -> None:
        cog = MagicMock()
        cog.is_lead.return_value = True
        view = RosterSettingsView(
            cog,
            7,
            is_open=True,
            buttons_hidden=False,
        )
        interaction = MagicMock()
        interaction.user = MagicMock()
        interaction.response.edit_message = AsyncMock()

        await view.run_action(interaction, "add")

        interaction.response.edit_message.assert_awaited_once()
        self.assertIsNone(interaction.response.edit_message.await_args.kwargs["content"])
        next_view = interaction.response.edit_message.await_args.kwargs["view"]
        self.assertIsInstance(next_view, RosterTargetMemberView)

    async def test_account_picker_uses_only_the_selector_prompt(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )
        repository.start_cycle(roster.id, "2026-07")
        account = LinkedAccount("#ABC", "Ahmad", "BEH", 18, 420)
        accounts = MagicMock()
        accounts.for_member.return_value = [account]
        cog = object.__new__(Rosters)
        cog.membership = _membership_service(repository, accounts)[0]
        interaction = MagicMock()
        interaction.user.id = 10
        interaction.response.defer = AsyncMock()
        interaction.edit_original_response = AsyncMock()

        with patch(
            "elbow_helper.features.rosters.services.membership.enrich_accounts",
            new=AsyncMock(return_value=[account]),
        ):
            await cog.show_account_picker(interaction, roster.id, mode="signup")

        interaction.response.defer.assert_awaited_once_with(
            ephemeral=True,
            thinking=True,
        )
        interaction.edit_original_response.assert_awaited_once()
        self.assertIsNone(
            interaction.edit_original_response.await_args.kwargs["content"]
        )
        view = interaction.edit_original_response.await_args.kwargs["view"]
        self.assertEqual(view.children[0].placeholder, "Select accounts")

    async def test_member_without_linked_accounts_keeps_management_controls(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
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
        accounts = MagicMock()
        accounts.for_member.return_value = []
        cog = object.__new__(Rosters)
        cog.membership = _membership_service(repository, accounts)[0]
        interaction = MagicMock()
        interaction.response.edit_message = AsyncMock()
        interaction.edit_original_response = AsyncMock()

        await cog.show_account_picker(
            interaction,
            roster.id,
            mode="signup",
            member_id=10,
            lead_override=True,
            edit_response=True,
        )

        interaction.response.edit_message.assert_awaited_once()
        progress = interaction.response.edit_message.await_args.kwargs["view"]
        self.assertIsInstance(progress, RosterProgressView)
        self.assertEqual(progress.children[0].label, "Loading accounts…")
        self.assertTrue(progress.children[0].disabled)
        interaction.edit_original_response.assert_awaited_once()
        response = interaction.edit_original_response.await_args.kwargs
        self.assertEqual(response["content"], "That member has no linked Clash accounts.")
        self.assertIsInstance(response["view"], RosterTargetMemberView)
        self.assertTrue(
            any(
                getattr(item, "placeholder", None) == "Select a member"
                for item in response["view"].children
            )
        )
        self.assertTrue(
            any(
                getattr(item, "label", None) == "Bulk add"
                for item in response["view"].children
            )
        )

    async def test_signup_result_replaces_the_picker_message(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=1209095400301133824,
            max_members=500,
        )
        roster, _ = repository.start_cycle(roster.id, "2026-07")
        account = LinkedAccount("#ABC", "Ahmad", "BEH", 18, 420)
        accounts = MagicMock()
        accounts.for_member.return_value = [account]
        membership, roles, _ = _membership_service(repository, accounts)
        cog = object.__new__(Rosters)
        cog.membership = membership
        interaction = MagicMock()
        interaction.response.edit_message = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        interaction.followup.send = AsyncMock()

        await cog.apply_account_selection(
            interaction,
            roster.id,
            member_id=10,
            player_tags=[account.player_tag],
            mode="signup",
            account_snapshots={account.player_tag: account},
        )

        interaction.response.edit_message.assert_awaited_once()
        progress = interaction.response.edit_message.await_args.kwargs["view"]
        self.assertEqual(progress.children[0].label, "Adding accounts…")
        self.assertTrue(progress.children[0].disabled)
        interaction.edit_original_response.assert_awaited_once_with(
            content="Added 1 account to CWL Sign-up.",
            view=None,
        )
        roles.sync.assert_awaited_once_with(roster, 10, should_have=True)

    async def test_bulk_add_uses_linked_accounts_and_reports_unmatched_tags(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
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
        temp_dir = tempfile.TemporaryDirectory()
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
        temp_dir = tempfile.TemporaryDirectory()
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

    async def test_render_fetches_and_persists_missing_hero_totals(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
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
            [{"player_tag": "#ABC", "player_name": "Ahmad", "townhall": 18}],
            roster.max_members,
        )
        cog = object.__new__(Rosters)
        bot = MagicMock()
        bot.get_guild.return_value = None
        emojis = MagicMock()
        emojis.get = AsyncMock(
            return_value=TownHallEmojiSet(header=None, levels={})
        )
        posts = RosterPostService(
            bot,
            repository,
            ClashClient(None),
            MagicMock(),
            cog,
            emojis,
        )
        enriched = LinkedAccount("#ABC", "Ahmad", "BEH", 18, 420)

        with patch(
            "elbow_helper.features.rosters.services.posts.enrich_accounts",
            new=AsyncMock(return_value=[enriched]),
        ):
            embeds, page, page_count = await posts.render(roster)

        self.assertEqual((page, page_count), (0, 1))
        self.assertNotIn("420", embeds[0].description)
        stored = repository.list_members(roster.id, roster.active_cycle_id)[0]
        self.assertEqual(stored.hero_sum, 420)

    async def test_refresh_reloads_current_clan_for_every_signed_up_account(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
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

    async def test_roster_autocomplete_shows_only_the_roster_name(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )
        cog = object.__new__(Rosters)
        cog._repository = repository
        interaction = MagicMock(guild_id=1)

        choices = await cog.roster_autocomplete(interaction, "CWL")

        self.assertEqual([choice.name for choice in choices], ["CWL Sign-up"])

    async def test_roster_autocomplete_reuses_the_guild_cache(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )
        cog = object.__new__(Rosters)
        cog._repository = MagicMock(wraps=repository)
        interaction = MagicMock(guild_id=1)

        first = await cog.roster_autocomplete(interaction, "CWL")
        second = await cog.roster_autocomplete(interaction, "Sign")

        self.assertEqual([choice.name for choice in first], ["CWL Sign-up"])
        self.assertEqual([choice.name for choice in second], ["CWL Sign-up"])
        cog._repository.list_rosters.assert_called_once_with(1)

    async def test_stale_roster_search_cache_remains_available_during_refresh_failure(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
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
        temp_dir = tempfile.TemporaryDirectory()
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
        temp_dir = tempfile.TemporaryDirectory()
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

    async def test_gear_menu_has_no_repeated_roster_title(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
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
        _wire_roster_service(cog, repository)
        interaction = MagicMock()
        interaction.response.send_message = AsyncMock()

        await cog.show_settings(interaction, roster.id)

        self.assertEqual(interaction.response.send_message.await_args.args, ())
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_direct_player_removal_updates_roles_and_one_message(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
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
        membership, roles, _ = _membership_service(
            repository,
            MagicMock(),
        )
        cog = object.__new__(Rosters)
        cog.membership = membership
        cog.is_lead = MagicMock(return_value=True)
        interaction = MagicMock()
        interaction.response.edit_message = AsyncMock()
        interaction.edit_original_response = AsyncMock()

        await cog.remove_roster_players(interaction, roster.id, ["#A"])

        remaining = repository.list_members(roster.id, roster.active_cycle_id)
        self.assertEqual([member.player_tag for member in remaining], ["#B"])
        roles.sync.assert_awaited_once_with(roster, 10, should_have=False)
        progress = interaction.response.edit_message.await_args.kwargs["view"]
        self.assertEqual(progress.children[0].label, "Removing accounts…")
        self.assertTrue(progress.children[0].disabled)
        interaction.edit_original_response.assert_awaited_once_with(
            content="Removed 1 account from **CWL Sign-up**.",
            view=None,
        )

    async def test_roster_opt_out_keeps_a_role_claimed_by_the_live_war_lineup(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
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
        cog.bot = MagicMock()
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
        temp_dir = tempfile.TemporaryDirectory()
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
        bot = MagicMock()
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
        temp_dir = tempfile.TemporaryDirectory()
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
        bot = MagicMock()
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
        temp_dir = tempfile.TemporaryDirectory()
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
        temp_dir = tempfile.TemporaryDirectory()
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
        temp_dir = tempfile.TemporaryDirectory()
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

    async def test_stable_roster_id_links_announcements_to_the_live_schedule(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        repository = RosterRepository(Path(temp_dir.name) / "rosters.sqlite3")
        roster = repository.create_roster(
            guild_id=1,
            name="CWL Sign-up",
            clan_code="FAMILY",
            role_id=None,
            max_members=500,
        )
        repository.configure_schedule(
            roster.id,
            enabled=True,
            timezone_name="UTC",
            open_day="18",
            open_time="11:00",
            close_day="last-1",
            close_time="21:00",
            reset_on_open=True,
        )
        cog = object.__new__(Rosters)
        _, automation, _, _ = _wire_roster_service(cog, repository)

        linked = await automation.cwl_signup_window(
            datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
        )

        self.assertIsNotNone(linked)
        linked_roster, window = linked
        self.assertEqual(linked_roster.id, roster.id)
        self.assertEqual(window.cycle_key, "2026-07")
        self.assertEqual(window.opens_at, datetime(2026, 7, 18, 11, 0, tzinfo=timezone.utc))
        self.assertEqual(window.closes_at, datetime(2026, 7, 30, 21, 0, tzinfo=timezone.utc))

    async def test_closed_schedule_embed_shows_the_next_opening(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
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
        bot = MagicMock()
        bot.get_guild.return_value = None
        emojis = MagicMock()
        emojis.get = AsyncMock(
            return_value=TownHallEmojiSet(header=None, levels={})
        )
        posts = RosterPostService(
            bot,
            repository,
            ClashClient(None),
            MagicMock(),
            cog,
            emojis,
        )

        with patch("elbow_helper.features.rosters.services.posts.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = datetime(
                2026,
                7,
                10,
                12,
                0,
                tzinfo=timezone.utc,
            )
            embeds, _, _ = await posts.render(roster)

        status = embeds[0].fields[0].value
        self.assertIn("Signup opens on", status)
        self.assertNotIn("Signup is **closed**", status)

    async def test_schedule_can_be_configured_before_a_roster_is_posted(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
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

    async def test_schedule_explains_why_days_after_28_are_not_accepted(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
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
        _wire_roster_service(cog, repository)
        cog._require_lead = AsyncMock(return_value=True)
        cog._resolve_roster = AsyncMock(return_value=roster)
        interaction = MagicMock()

        with patch("elbow_helper.features.rosters.cog.warn", new=AsyncMock()) as mocked_warn:
            await cog.roster_schedule(
                interaction,
                str(roster.id),
                open_day="30",
            )

        mocked_warn.assert_awaited_once_with(
            interaction,
            "Day 30 isn't available every month. Use `last`, `last-1`, or `last-2` "
            "for month-end timing.",
        )

    async def test_roster_export_builds_an_xlsx_snapshot_with_current_terms(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
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
            return_value=(
                "https://docs.google.com/spreadsheets/d/sheet-id/edit",
                None,
            )
        )
        writer = MagicMock()
        bot = MagicMock()
        bot.get_guild.return_value = None
        profiles = RosterProfileService(repository, ClashClient(None))
        sheet_publisher = RosterSheetPublisher(
            bot,
            repository,
            profiles,
            publisher,
            writer,
            LocalExportStore(Path(temp_dir.name)),
        )

        report, warning = await sheet_publisher.export(roster)

        self.assertIsNotNone(report)
        self.assertIsNone(warning)
        rows = writer.write.call_args.args[1][0][1]
        self.assertEqual(
            rows[0],
            [
                "Discord Member",
                "Account",
                "Player Tag",
                "TH",
                "Combined Hero Level",
                "Current Clan",
                "Signed Up",
            ],
        )
        self.assertEqual(rows[1][1:6], ["Ahmad", "#ABC", 18, 420, "BEH"])
        publisher.upload_workbook.assert_awaited_once_with(
            report.workbook_path,
            ANY,
        )

    async def test_roster_export_uses_refreshed_profiles_without_saving_a_sheet_id(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
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
                "player_name": "Old Name",
                "clan_code": "BEH",
                "townhall": 17,
                "hero_sum": 400,
            }],
            roster.max_members,
        )
        publisher = MagicMock()
        publisher.upload_workbook = AsyncMock(
            return_value=(
                "https://docs.google.com/spreadsheets/d/replacement-sheet/edit",
                None,
            )
        )
        writer = MagicMock()
        bot = MagicMock()
        bot.get_guild.return_value = None
        profiles = RosterProfileService(repository, ClashClient(None))
        sheet_publisher = RosterSheetPublisher(
            bot,
            repository,
            profiles,
            publisher,
            writer,
            LocalExportStore(Path(temp_dir.name)),
        )
        refreshed = LinkedAccount("#ABC", "Current Name", "BE4", 18, 450)

        with patch(
            "elbow_helper.features.rosters.services.profiles.enrich_accounts",
            new=AsyncMock(return_value=[refreshed]),
        ):
            report, warning = await sheet_publisher.export(roster)

        self.assertIsNotNone(report)
        self.assertEqual(
            report.google_link,
            "https://docs.google.com/spreadsheets/d/replacement-sheet/edit",
        )
        self.assertIsNone(warning)
        rows = writer.write.call_args.args[1][0][1]
        self.assertEqual(rows[1][1:6], [
            "Current Name",
            "#ABC",
            18,
            450,
            "BE4",
        ])
        stored = repository.get_roster(roster.id)
        self.assertIsNotNone(stored)
        self.assertFalse(hasattr(stored, "google_sheet_id"))

    async def test_roster_export_creates_a_valid_xlsx_fallback(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
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
            MagicMock(),
            repository,
            RosterProfileService(repository, ClashClient(None)),
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
        temp_dir = tempfile.TemporaryDirectory()
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
        bot = MagicMock()
        profiles = RosterProfileService(repository, ClashClient(None))
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


class RosterCommandMetadataTests(unittest.TestCase):
    def test_account_counts_use_the_right_singular_and_plural(self) -> None:
        self.assertEqual(account_count(1), "1 account")
        self.assertEqual(account_count(2), "2 accounts")

    def test_only_the_name_constraint_is_reported_as_an_existing_roster(self) -> None:
        duplicate = sqlite3.IntegrityError(
            "UNIQUE constraint failed: rosters.guild_id, rosters.name"
        )
        other_failure = sqlite3.IntegrityError(
            "NOT NULL constraint failed: rosters.min_townhall"
        )

        self.assertTrue(_is_roster_name_conflict(duplicate))
        self.assertFalse(_is_roster_name_conflict(other_failure))

    def test_limits_and_descriptions_are_visible_in_discord(self) -> None:
        cog = object.__new__(Rosters)
        create = app_commands.Command(
            name="create",
            description="Set up a roster members can join with linked Clash accounts.",
            callback=cog.roster_create,
        )
        edit = app_commands.Command(
            name="edit",
            description="Update a roster's settings.",
            callback=cog.roster_edit,
        )
        create_options = {option.name: option for option in create.parameters}
        edit_options = {option.name: option for option in edit.parameters}

        self.assertEqual(create_options["max_members"].max_value, 500)
        self.assertIn("defaults to 500", create_options["max_members"].description)
        self.assertNotIn("min_townhall", create_options)
        self.assertIn("min_townhall", edit_options)
        self.assertNotIn("remove_min_townhall", edit_options)
        self.assertFalse(edit_options["min_townhall"].required)
        self.assertEqual(edit_options["min_townhall"].min_value, 0)
        self.assertIn("0 for no minimum", edit_options["min_townhall"].description)
        self.assertEqual(
            create_options["clan"].description,
            "Show the roster for the full clan family or a single clan.",
        )
        self.assertEqual(
            edit_options["clan"].description,
            "Show the roster for the full clan family or a single clan.",
        )

    def test_schedule_uses_free_text_day_rules_without_day_autocomplete(self) -> None:
        cog = object.__new__(Rosters)
        schedule = app_commands.Command(
            name="schedule",
            description="Set automatic monthly opening and closing times for a roster.",
            callback=cog.roster_schedule,
        )
        options = {option.name: option for option in schedule.parameters}

        self.assertFalse(options["open_day"].required)
        self.assertFalse(options["close_day"].required)
        self.assertFalse(options["open_day"].autocomplete)
        self.assertFalse(options["close_day"].autocomplete)
        self.assertIn("last-2", options["open_day"].description)
        self.assertIn("last-2", options["close_day"].description)
        self.assertNotIn("last-N", options["open_day"].description)
        self.assertNotIn("last-N", options["close_day"].description)
        self.assertNotIn("last-x", options["open_day"].description)
        self.assertNotIn("last-x", options["close_day"].description)
        self.assertTrue(options["timezone"].autocomplete)
        self.assertEqual(
            options["timezone"].description,
            "Timezone for the opening and closing times.",
        )
        self.assertEqual(
            options["reset_on_open"].description,
            "Clear existing signups each time the roster opens.",
        )

    def test_post_option_asks_only_for_the_roster(self) -> None:
        cog = object.__new__(Rosters)
        post = app_commands.Command(
            name="post",
            description="Post a roster in this channel.",
            callback=cog.roster_post,
        )

        self.assertEqual(post.parameters[0].description, "Roster to post.")

    def test_clone_exposes_only_reusable_setting_overrides(self) -> None:
        cog = object.__new__(Rosters)
        clone = app_commands.Command(
            name="clone",
            description="Create a roster using another roster's settings.",
            callback=cog.roster_clone,
        )
        options = {option.name: option for option in clone.parameters}

        self.assertTrue(options["roster"].required)
        self.assertTrue(options["name"].required)
        for name in ("clan", "signup_role", "max_members", "min_townhall"):
            self.assertFalse(options[name].required)
        self.assertIn("source roster's clan", options["clan"].description)
        self.assertIn("source roster's role", options["signup_role"].description)
        self.assertIn("source roster's limit", options["max_members"].description)
        self.assertIn("enter 0 for none", options["min_townhall"].description)

    def test_internal_permission_names_are_not_user_facing(self) -> None:
        package_root = Path(elbow_helper.__file__).parent
        public_sources = (
            package_root / "features" / "rosters" / "cog.py",
            package_root / "features" / "rosters" / "ui" / "views.py",
            package_root / "features" / "help" / "catalog.py",
        )
        visible_text = "\n".join(path.read_text(encoding="utf-8") for path in public_sources)

        self.assertNotIn("Lead+", visible_text)


class RosterProfileTests(unittest.TestCase):
    def test_player_payload_adds_clan_townhall_and_home_heroes(self) -> None:
        account = LinkedAccount("#ABC", "Old", "", 0)
        enriched = _account_from_payload(
            account,
            {
                "name": "Ahmad",
                "townHallLevel": 18,
                "clan": {"tag": "#2Y2PJCVGU", "name": "Hellbow"},
                "heroes": [
                    {"name": "Barbarian King", "level": 100, "village": "home"},
                    {"name": "Battle Machine", "level": 35, "village": "builderBase"},
                ],
            },
        )

        self.assertEqual(enriched.player_name, "Ahmad")
        self.assertEqual(enriched.clan_code, "BEH")
        self.assertEqual(enriched.townhall, 18)
        self.assertEqual(enriched.hero_sum, 100)
        self.assertEqual(enriched.hero_levels, (("BK", 100),))

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
