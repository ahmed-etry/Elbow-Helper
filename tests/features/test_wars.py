from __future__ import annotations

import asyncio
import unittest
from copy import deepcopy
from datetime import datetime
from datetime import timezone
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import discord

from elbow_helper.features.rosters.ui.emojis import TownHallEmojiProvider
from elbow_helper.features.wars.board import WarBoardMixin
from elbow_helper.features.wars.board import PreviousWarView
from elbow_helper.features.wars.emojis import required_war_emoji_names
from elbow_helper.features.wars.emojis import WarEmojiProvider
from elbow_helper.features.wars.emojis import WarEmojiSet
from elbow_helper.features.wars.config import NOTICE_TTL
from elbow_helper.features.wars.config import WAR_BOARD_CLAN_CODES
from elbow_helper.features.wars.helpers import HelperMixin
from elbow_helper.features.wars.rendering import build_war_board_embed
from elbow_helper.features.wars.rendering import build_war_summary_embed
from elbow_helper.features.wars.roles import WarRoleMixin
from elbow_helper.features.wars.state import StateMixin
from elbow_helper.features.wars.state import save_cache
from elbow_helper.features.wars.tasks import TaskMixin
from elbow_helper.features.wars.warflow import WarflowMixin
from elbow_helper.configuration.clans import CLAN_WAR_ROLE_IDS
from elbow_helper.configuration.channels import CLAN_WAR_CHANNELS


def _war_payload(
    state: str = "inWar",
    *,
    team_size: int = 2,
    attacks_per_member: int = 2,
) -> dict[str, object]:
    clan_members = []
    opponent_members = []
    order = 1
    for position in range(1, team_size + 1):
        defender_tag = f"#D{position}"
        attacks = []
        for attack_number in range(attacks_per_member):
            attacks.append(
                {
                    "attackerTag": f"#A{position}",
                    "defenderTag": defender_tag,
                    "stars": min(3, attack_number + 2),
                    "destructionPercentage": 80 + attack_number * 20,
                    "order": order,
                }
            )
            order += 1
        clan_members.append(
            {
                "tag": f"#A{position}",
                "name": f"Player {position}",
                "townhallLevel": 18 if position == 1 else 17,
                "mapPosition": position,
                "attacks": attacks,
            }
        )
        opponent_members.append(
            {
                "tag": defender_tag,
                "name": f"Opponent {position}",
                "townhallLevel": 18 if position == 1 else 17,
                "mapPosition": position,
                "attacks": [],
            }
        )
    return {
        "state": state,
        "teamSize": team_size,
        "attacksPerMember": attacks_per_member,
        "preparationStartTime": "20260720T210000.000Z",
        "startTime": "20260721T210000.000Z",
        "endTime": "20260722T210000.000Z",
        "clan": {
            "tag": "#2Y2PJCVGU",
            "name": "Hellbow",
            "badgeUrls": {"small": "https://example.com/hellbow.png"},
            "stars": team_size * 3,
            "attacks": team_size * attacks_per_member,
            "destructionPercentage": 100.0,
            "members": clan_members,
        },
        "opponent": {
            "tag": "#RIVAL",
            "name": "Rival Clan",
            "badgeUrls": {"small": "https://example.com/rival.png"},
            "stars": team_size,
            "attacks": 0,
            "destructionPercentage": 50.0,
            "members": opponent_members,
        },
    }


def _war_emojis() -> WarEmojiSet:
    base_id = 1234567890123456000
    return WarEmojiSet(
        icons={
            "yellow_star": f"<:war_yellow_star:{base_id + 1}>",
            "sword": f"<:war_sword:{base_id + 4}>",
            "fire": f"<:war_fire:{base_id + 5}>",
        },
        town_halls={
            17: f"<:th17:{base_id + 17}>",
            18: f"<:th18:{base_id + 18}>",
        },
        numbers={
            number: f"<:war_number_{number}:{base_id + 200 + number}>"
            for number in range(0, 51)
        },
    )


class WarSummaryCleanupTests(unittest.TestCase):
    def test_final_war_summary_expires_after_48_hours(self) -> None:
        self.assertEqual(NOTICE_TTL, 48 * 60 * 60)


class _WarRoleManager(WarRoleMixin, StateMixin):
    pass


def _war_role_manager(
    *,
    linked_accounts: dict[str, int],
    members: dict[int, MagicMock],
    roster_claim: bool = False,
) -> tuple[_WarRoleManager, MagicMock, MagicMock]:
    manager = _WarRoleManager()
    manager.cache = {}
    manager.war_role_lineups = {}
    manager.war_role_managed_members = {}
    manager._war_role_locks = {}
    manager._war_role_missing_links = {}
    manager._save_cache_async = AsyncMock()

    role = MagicMock(id=CLAN_WAR_ROLE_IDS["BEH"])
    guild = MagicMock()
    guild.get_role.return_value = role
    guild.get_member.side_effect = members.get
    guild.fetch_member = AsyncMock()

    clan_links = MagicMock()
    clan_links.get_all_links.return_value = {
        tag: {"discord_user_id": member_id}
        for tag, member_id in linked_accounts.items()
    }
    roster_role_claim = AsyncMock(return_value=roster_claim)
    manager._roster_role_claim = roster_role_claim

    manager.bot = MagicMock()
    manager.bot.get_guild.return_value = guild
    manager.account_links = clan_links
    return manager, role, roster_role_claim


class WarRoleSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_preparation_lineup_grants_and_persists_the_war_role(self) -> None:
        member = MagicMock()
        member.roles = []
        member.add_roles = AsyncMock()
        member.remove_roles = AsyncMock()
        manager, role, _ = _war_role_manager(
            linked_accounts={"#P2": 42},
            members={42: member},
        )
        payload = _war_payload("preparation", team_size=1)
        payload["clan"]["members"][0]["tag"] = "#P2"

        await manager._sync_war_roles("Hellbow", payload)

        member.add_roles.assert_awaited_once_with(
            role,
            reason="Current regular-war lineup: BEH",
        )
        self.assertEqual(manager.war_role_lineups, {"BEH": {"#P2": 42}})
        self.assertEqual(manager.war_role_managed_members, {"BEH": {42}})
        self.assertEqual(
            manager.cache["war_role_state"]["BEH"],
            {"lineup": {"#P2": 42}, "managed": [42]},
        )

    async def test_war_end_removes_only_the_live_lineup_claim(self) -> None:
        member = MagicMock()
        member.add_roles = AsyncMock()
        member.remove_roles = AsyncMock()
        manager, role, repository = _war_role_manager(
            linked_accounts={},
            members={42: member},
        )
        member.roles = [role]
        manager.war_role_lineups = {"BEH": {"#P2": 42}}
        manager.war_role_managed_members = {"BEH": {42}}

        await manager._sync_war_roles("Hellbow", {"state": "warEnded"})

        repository.assert_awaited_once_with(
            CLAN_WAR_ROLE_IDS["BEH"],
            42,
        )
        member.remove_roles.assert_awaited_once_with(
            role,
            reason="No longer in BEH regular-war lineup",
        )
        self.assertEqual(manager.war_role_lineups, {})
        self.assertEqual(manager.war_role_managed_members, {})
        self.assertNotIn("war_role_state", manager.cache)

    async def test_roster_signup_keeps_the_role_when_the_war_ends(self) -> None:
        member = MagicMock()
        member.roles = []
        member.add_roles = AsyncMock()
        member.remove_roles = AsyncMock()
        manager, role, _ = _war_role_manager(
            linked_accounts={},
            members={42: member},
            roster_claim=True,
        )
        member.roles = [role]
        manager.war_role_lineups = {"BEH": {"#P2": 42}}
        manager.war_role_managed_members = {"BEH": {42}}

        await manager._sync_war_roles("Hellbow", {"state": "warEnded"})

        member.remove_roles.assert_not_awaited()
        self.assertEqual(manager.war_role_lineups, {})
        self.assertEqual(manager.war_role_managed_members, {})

    async def test_cwl_and_no_war_expire_regular_war_lineups(self) -> None:
        for payload in (
            {"state": "preparation", "warTag": "#CWL"},
            {"state": "notInWar"},
        ):
            with self.subTest(payload=payload):
                member = MagicMock()
                member.add_roles = AsyncMock()
                member.remove_roles = AsyncMock()
                manager, role, _ = _war_role_manager(
                    linked_accounts={},
                    members={42: member},
                )
                member.roles = [role]
                manager.war_role_lineups = {"BEH": {"#P2": 42}}
                manager.war_role_managed_members = {"BEH": {42}}

                await manager._sync_war_roles("Hellbow", payload)

                member.remove_roles.assert_awaited_once()

    async def test_incomplete_active_payload_never_removes_roles(self) -> None:
        member = MagicMock()
        member.roles = []
        member.add_roles = AsyncMock()
        member.remove_roles = AsyncMock()
        manager, _, repository = _war_role_manager(
            linked_accounts={},
            members={42: member},
        )
        manager.war_role_lineups = {"BEH": {"#P2": 42}}
        manager.war_role_managed_members = {"BEH": {42}}

        await manager._sync_war_roles(
            "Hellbow",
            {"state": "inWar", "clan": {"members": []}},
        )

        member.remove_roles.assert_not_awaited()
        repository.assert_not_awaited()
        self.assertEqual(manager.war_role_lineups, {"BEH": {"#P2": 42}})

    def test_war_role_claims_restore_from_cache(self) -> None:
        manager = _WarRoleManager()
        manager.cache = {
            "war_role_state": {
                "BEH": {"lineup": {"#P2": 42}, "managed": [42, 43]},
            }
        }

        lineups, managed = manager._load_war_role_state()

        self.assertEqual(lineups, {"BEH": {"#P2": 42}})
        self.assertEqual(managed, {"BEH": {42, 43}})


class WarChannelRetentionTests(unittest.TestCase):
    def test_legacy_clan_war_channel_cleanup_is_retired(self) -> None:
        self.assertFalse(hasattr(HelperMixin, "_cleanup_ended_war_embeds"))
        self.assertFalse(hasattr(TaskMixin, "_month_end_war_cleanup_loop"))


class WarSummaryReplacementTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_summary_removes_the_previous_one_in_its_channel(self) -> None:
        manager = TaskMixin()
        manager.summary_registry = {
            "100": {"channel": 7, "sent_at": 1},
            "200": {"channel": 7, "sent_at": 2},
            "300": {"channel": 8, "sent_at": 3},
        }
        manager.cache = {"summary_messages": dict(manager.summary_registry)}
        manager._save_cache_async = AsyncMock()
        previous = MagicMock()
        previous.delete = AsyncMock()
        channel = MagicMock()
        channel.id = 7
        channel.fetch_message = AsyncMock(return_value=previous)

        await manager._cleanup_previous_summary_messages(
            channel,
            keep_message_id=200,
        )

        channel.fetch_message.assert_awaited_once_with(100)
        previous.delete.assert_awaited_once_with()
        self.assertEqual(
            manager.summary_registry,
            {
                "200": {"channel": 7, "sent_at": 2},
                "300": {"channel": 8, "sent_at": 3},
            },
        )
        self.assertEqual(manager.cache["summary_messages"], manager.summary_registry)
        manager._save_cache_async.assert_awaited_once_with()


async def _messages(messages):
    for message in messages:
        yield message


class _WarSummaryManager(WarflowMixin, HelperMixin, StateMixin, TaskMixin):
    pass


def _war_summary_manager() -> tuple[_WarSummaryManager, MagicMock, MagicMock]:
    manager = _WarSummaryManager()
    manager.cache = {}
    manager.summary_registry = {}
    manager.processed_war_order = []
    manager.processed_war_ids = set()
    manager.war_context = {}
    manager._war_state_locks = {}
    manager._war_summary_state_lock = asyncio.Lock()
    manager._wars_in_flight = {}
    manager.clan_channels = {
        "Hellbow": {"leadership_channel": 7, "leadership_role": 8}
    }
    manager._save_cache_async = AsyncMock()
    manager.war_emojis = MagicMock()
    manager.war_emojis.get = AsyncMock(return_value=_war_emojis())

    channel = MagicMock()
    channel.id = 7
    channel.send = AsyncMock()
    channel.fetch_message = AsyncMock()
    channel.history.side_effect = lambda **kwargs: _messages([])

    message = MagicMock()
    message.id = 900
    message.channel = channel
    message.created_at = datetime(2026, 7, 22, 21, 1, tzinfo=timezone.utc)
    message.author.id = 99
    channel.send.return_value = message

    manager.bot = MagicMock()
    manager.bot.user.id = 99
    manager.bot.get_channel.return_value = channel
    manager.bot.fetch_channel = AsyncMock(return_value=channel)
    return manager, channel, message


class WarSummaryDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_is_persisted_before_older_posts_are_cleaned(self) -> None:
        manager, channel, message = _war_summary_manager()
        events: list[str] = []

        async def save() -> None:
            events.append("save")

        async def send(**kwargs):
            events.append("send")
            return message

        async def cleanup(*args, **kwargs) -> None:
            events.append("cleanup")

        manager._save_cache_async.side_effect = save
        manager._cleanup_previous_summary_messages = AsyncMock(
            side_effect=cleanup
        )
        channel.send.side_effect = send

        await manager._handle_war_state("Hellbow", _war_payload("warEnded"))

        self.assertEqual(events, ["send", "save", "cleanup"])
        channel.send.assert_awaited_once()
        self.assertEqual(manager.summary_registry["900"]["channel"], 7)
        self.assertEqual(len(manager.processed_war_ids), 1)

    async def test_restart_adopts_an_existing_summary_without_posting_again(self) -> None:
        manager, channel, message = _war_summary_manager()
        ended_at = datetime(2026, 7, 22, 21, 0, tzinfo=timezone.utc)
        embed = build_war_summary_embed(
            _war_payload("warEnded"),
            _war_emojis(),
            timestamp=ended_at,
        )
        message.embeds = [embed]
        channel.history.side_effect = lambda **kwargs: _messages([message])
        war_id = manager._build_war_id(_war_payload("warEnded"))

        await manager._handle_war_state("Hellbow", _war_payload("warEnded"))

        channel.send.assert_not_awaited()
        self.assertIn(war_id, manager.processed_war_ids)
        self.assertEqual(manager.summary_registry["900"]["channel"], 7)
        manager._save_cache_async.assert_awaited_once_with()

    async def test_summary_state_write_failure_is_reconciled_without_resending(self) -> None:
        manager, channel, message = _war_summary_manager()
        manager._save_cache_async.side_effect = OSError("disk full")
        war_id = manager._build_war_id(_war_payload("warEnded"))

        with self.assertRaises(OSError):
            await manager._handle_war_state("Hellbow", _war_payload("warEnded"))

        channel.send.assert_awaited_once()
        self.assertNotIn(war_id, manager.processed_war_ids)
        self.assertNotIn("900", manager.summary_registry)

        ended_at = datetime(2026, 7, 22, 21, 0, tzinfo=timezone.utc)
        message.embeds = [
            build_war_summary_embed(
                _war_payload("warEnded"),
                _war_emojis(),
                timestamp=ended_at,
            )
        ]
        channel.history.side_effect = lambda **kwargs: _messages([message])
        manager._save_cache_async.side_effect = None

        await manager._handle_war_state("Hellbow", _war_payload("warEnded"))

        channel.send.assert_awaited_once()
        self.assertIn(war_id, manager.processed_war_ids)
        self.assertEqual(manager.summary_registry["900"]["channel"], 7)


class WarCachePersistenceTests(unittest.TestCase):
    def test_cache_write_failures_are_not_swallowed(self) -> None:
        with (
            patch(
                "elbow_helper.features.wars.state.write_json_atomic",
                side_effect=OSError("disk full"),
            ),
            self.assertRaises(OSError),
        ):
            save_cache({"processed_wars": []})


class WarBoardRenderingTests(unittest.TestCase):
    def test_live_board_is_enabled_for_regular_war_clans(self) -> None:
        self.assertEqual(
            WAR_BOARD_CLAN_CODES,
            ("BEH", "BE4", "BES", "BE1", "BEM", "BEC", "BEE"),
        )

    def test_live_board_matches_the_clashperk_war_structure(self) -> None:
        embed = build_war_board_embed(_war_payload(), _war_emojis())

        self.assertEqual(embed.title, "Hellbow (#2Y2PJCVGU)")
        self.assertIn("tag=%232Y2PJCVGU", embed.url)
        self.assertEqual(embed.thumbnail.url, "https://example.com/hellbow.png")
        self.assertIn("**War Against**", embed.description)
        self.assertIn("**War State**\nBattle Day", embed.description)
        self.assertIn("**War Size**\n2 vs 2", embed.description)
        self.assertIn("**War Stats**", embed.description)
        self.assertIn("0 left", embed.description)
        self.assertIn("4 left", embed.description)
        self.assertIn("**Rosters**", embed.description)
        self.assertIn("<:th18:", embed.description)
        self.assertFalse(embed.fields)
        self.assertLessEqual(len(embed.description), 4096)
        self.assertLessEqual(len(embed), 6000)

    def test_preparation_and_ended_states_show_their_relevant_content(self) -> None:
        preparation = build_war_board_embed(
            _war_payload("preparation"),
            _war_emojis(),
        )
        ended = build_war_board_embed(_war_payload("warEnded"), _war_emojis())

        self.assertIn("Preparation Day", preparation.description)
        self.assertIn("War Start Time:", preparation.description)
        self.assertNotIn("**War Stats**", preparation.description)
        self.assertFalse(preparation.fields)
        self.assertIn("War Ended — Victory", ended.description)
        self.assertIsNone(ended.footer.text)
        self.assertIn("**War Stats**", ended.description)

    def test_leadership_summary_matches_the_war_board_stats_presentation(self) -> None:
        ended_at = datetime(2026, 7, 22, 21, 0, tzinfo=timezone.utc)
        summary = build_war_summary_embed(
            _war_payload("warEnded"),
            _war_emojis(),
            timestamp=ended_at,
        )

        self.assertEqual(summary.title, "Clan War Ended")
        self.assertIn("**War Against**", summary.description)
        self.assertIn("Rival Clan (#RIVAL)", summary.description)
        self.assertIn("**War Stats**", summary.description)
        self.assertIn("<:war_yellow_star:", summary.description)
        self.assertIn("<:war_sword:", summary.description)
        self.assertIn("<:war_fire:", summary.description)
        self.assertIn("4 left", summary.description)
        self.assertEqual(summary.timestamp, ended_at)
        self.assertEqual(
            summary.author.name,
            "Hellbow",
        )
        self.assertEqual(
            summary.author.icon_url,
            "https://example.com/hellbow.png",
        )
        self.assertIn("tag=%232Y2PJCVGU", summary.author.url)
        self.assertIsNone(summary.thumbnail.url)

    def test_missed_attack_copy_uses_the_attack_count(self) -> None:
        payload = _war_payload("warEnded")
        payload["clan"]["members"][0]["attacks"] = payload["clan"]["members"][0]["attacks"][:1]
        payload["clan"]["members"][1]["attacks"] = []
        helper = HelperMixin()

        missed = helper._compute_missed(payload)

        self.assertEqual(
            missed,
            ["Player 1 — 1 attack", "Player 2 — 2 attacks"],
        )

class WarEmojiTests(unittest.IsolatedAsyncioTestCase):
    async def test_rosters_and_wars_share_one_application_emoji_fetch(self) -> None:
        names = {
            "town_hall",
            *(f"th{level}" for level in range(1, 19)),
            *required_war_emoji_names(),
        }
        client = MagicMock()
        client.fetch_application_emojis = AsyncMock(
            return_value=[
                discord.PartialEmoji(name=name, id=index + 1000)
                for index, name in enumerate(sorted(names))
            ]
        )
        roster_provider = TownHallEmojiProvider(client)
        war_provider = WarEmojiProvider(client)

        roster_emojis = await roster_provider.get()
        war_emojis = await war_provider.get()

        self.assertTrue(roster_emojis.is_complete)
        self.assertTrue(war_emojis.is_complete)
        client.fetch_application_emojis.assert_awaited_once_with()


class WarBoardLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_wars_edit_the_registered_message_instead_of_posting_again(self) -> None:
        manager = WarBoardMixin()
        manager.war_board_registry = {}
        manager.cache = {}
        manager._save_cache_async = AsyncMock()
        manager.war_emojis = MagicMock()
        manager.war_emojis.get = AsyncMock(return_value=_war_emojis())

        channel = MagicMock()
        channel.id = CLAN_WAR_CHANNELS["BEH"]
        channel.fetch_message = AsyncMock()
        posted = MagicMock()
        posted.id = 900
        posted.channel = channel
        posted.edit = AsyncMock()
        channel.send = AsyncMock(return_value=posted)
        manager.bot = MagicMock()
        manager.bot.get_channel.return_value = channel

        first_war = _war_payload()
        await manager._update_war_board("Hellbow", first_war)

        posted.embeds = [build_war_board_embed(first_war, _war_emojis())]
        posted.components = [MagicMock()]
        channel.fetch_message.return_value = posted
        next_war = deepcopy(first_war)
        next_war["opponent"]["name"] = "Next Rival"
        next_war["opponent"]["tag"] = "#NEXT"
        await manager._update_war_board("Hellbow", next_war)

        channel.send.assert_awaited_once()
        posted.edit.assert_awaited_once()
        self.assertEqual(
            manager.war_board_registry["BEH"],
            {"channel": channel.id, "message": posted.id},
        )

    async def test_unchanged_board_is_not_edited_or_posted_again(self) -> None:
        manager = WarBoardMixin()
        manager.war_board_registry = {
            "BEH": {"channel": CLAN_WAR_CHANNELS["BEH"], "message": 900}
        }
        manager.cache = {"war_board_messages": manager.war_board_registry}
        manager._save_cache_async = AsyncMock()
        manager.war_emojis = MagicMock()
        manager.war_emojis.get = AsyncMock(return_value=_war_emojis())

        data = _war_payload()
        existing = build_war_board_embed(data, _war_emojis())
        existing.timestamp = discord.utils.utcnow()
        message = MagicMock()
        message.id = 900
        message.embeds = [existing]
        message.edit = AsyncMock()
        message.components = []
        channel = MagicMock()
        channel.id = CLAN_WAR_CHANNELS["BEH"]
        channel.fetch_message = AsyncMock(return_value=message)
        channel.send = AsyncMock()
        manager.bot = MagicMock()
        manager.bot.get_channel.return_value = channel

        await manager._update_war_board("Hellbow", data)

        channel.send.assert_not_awaited()
        message.edit.assert_not_awaited()

    async def test_existing_controls_are_removed_on_refresh(self) -> None:
        manager = WarBoardMixin()
        manager.war_board_registry = {
            "BEH": {"channel": CLAN_WAR_CHANNELS["BEH"], "message": 900}
        }
        manager.cache = {"war_board_messages": manager.war_board_registry}
        manager._save_cache_async = AsyncMock()
        manager.war_emojis = MagicMock()
        manager.war_emojis.get = AsyncMock(return_value=_war_emojis())

        data = _war_payload()
        message = MagicMock()
        message.id = 900
        message.embeds = [build_war_board_embed(data, _war_emojis())]
        message.components = [MagicMock()]
        message.edit = AsyncMock()
        channel = MagicMock()
        channel.id = CLAN_WAR_CHANNELS["BEH"]
        channel.fetch_message = AsyncMock(return_value=message)
        channel.send = AsyncMock()
        manager.bot = MagicMock()
        manager.bot.get_channel.return_value = channel

        await manager._update_war_board("Hellbow", data)

        channel.send.assert_not_awaited()
        message.edit.assert_awaited_once()
        self.assertIsNone(message.edit.await_args.kwargs["view"])

    async def test_next_war_adds_access_to_the_completed_previous_war(self) -> None:
        manager = WarBoardMixin()
        manager.war_board_registry = {}
        manager.war_board_history = {}
        manager.cache = {}
        manager._save_cache_async = AsyncMock()
        manager.war_emojis = MagicMock()
        manager.war_emojis.get = AsyncMock(return_value=_war_emojis())

        channel = MagicMock()
        channel.id = CLAN_WAR_CHANNELS["BEH"]
        posted = MagicMock()
        posted.id = 900
        posted.channel = channel
        posted.edit = AsyncMock()
        channel.send = AsyncMock(return_value=posted)
        channel.fetch_message = AsyncMock(return_value=posted)
        manager.bot = MagicMock()
        manager.bot.get_channel.return_value = channel

        ended = _war_payload("warEnded")
        await manager._update_war_board("Hellbow", ended)

        posted.embeds = [build_war_board_embed(ended, _war_emojis())]
        posted.components = []
        upcoming = _war_payload("preparation")
        upcoming["preparationStartTime"] = "20260722T210000.000Z"
        upcoming["startTime"] = "20260723T210000.000Z"
        upcoming["endTime"] = "20260724T210000.000Z"
        upcoming["opponent"]["tag"] = "#NEXT"
        upcoming["opponent"]["name"] = "Next Rival"

        await manager._update_war_board("Hellbow", upcoming)

        previous = manager.war_board_history["BEH"]["previous"]
        self.assertEqual(previous["state"], "warEnded")
        self.assertEqual(previous["opponent"]["tag"], "#RIVAL")
        self.assertIsInstance(posted.edit.await_args.kwargs["view"], PreviousWarView)

    async def test_missed_final_poll_uses_war_log_result_with_captured_lineups(
        self,
    ) -> None:
        manager = WarBoardMixin()
        live = _war_payload("inWar")
        manager.war_board_history = {"BEH": {"current": live}}
        manager.cache = {"war_board_history": manager.war_board_history}
        manager._save_cache_async = AsyncMock()
        final_result = deepcopy(live)
        final_result["clan"].pop("members")
        final_result["opponent"].pop("members")
        final_result["clan"]["stars"] = 10
        final_result["opponent"]["stars"] = 8
        manager._fetch_war_log_result = AsyncMock(return_value=final_result)

        upcoming = _war_payload("preparation")
        upcoming["preparationStartTime"] = "20260722T210000.000Z"
        upcoming["startTime"] = "20260723T210000.000Z"
        upcoming["endTime"] = "20260724T210000.000Z"
        upcoming["opponent"]["tag"] = "#NEXT"

        await manager._record_war_board_snapshot("Hellbow", "BEH", upcoming)

        previous = manager.war_board_history["BEH"]["previous"]
        self.assertEqual(previous["state"], "warEnded")
        self.assertEqual(previous["clan"]["stars"], 10)
        self.assertEqual(len(previous["clan"]["members"]), 2)
        manager._fetch_war_log_result.assert_awaited_once_with("Hellbow", live)

    async def test_previous_war_button_returns_the_ended_embed_privately(self) -> None:
        manager = WarBoardMixin()
        previous = _war_payload("warEnded")
        manager.war_board_history = {"BEH": {"previous": previous}}
        manager.war_emojis = MagicMock()
        manager.war_emojis.get = AsyncMock(return_value=_war_emojis())
        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        view = PreviousWarView(manager, "BEH")

        await view.children[0].callback(interaction)

        interaction.response.defer.assert_awaited_once_with(
            ephemeral=True,
            thinking=True,
        )
        response = interaction.edit_original_response.await_args.kwargs
        self.assertIn("War Ended", response["embed"].description)
        self.assertEqual(
            response["embed"].timestamp,
            discord.utils.parse_time("2026-07-22T21:00:00+00:00"),
        )

    async def test_previous_war_button_is_restored_after_reboot(self) -> None:
        manager = WarBoardMixin()
        manager.war_board_registry = {
            "BEH": {"channel": CLAN_WAR_CHANNELS["BEH"], "message": 900}
        }
        manager.war_board_history = {
            "BEH": {
                "current": _war_payload("preparation"),
                "previous": _war_payload("warEnded"),
            }
        }
        manager.bot = MagicMock()

        manager._register_war_board_views()

        manager.bot.add_view.assert_called_once()
        view = manager.bot.add_view.call_args.args[0]
        self.assertIsInstance(view, PreviousWarView)
        self.assertEqual(manager.bot.add_view.call_args.kwargs["message_id"], 900)


class WarBoardHistoryStateTests(unittest.TestCase):
    def test_history_loader_discards_malformed_snapshots(self) -> None:
        manager = StateMixin()
        valid = _war_payload("warEnded")
        manager.cache = {
            "war_board_history": {
                "BEH": {"current": valid, "previous": "bad"},
                "BAD": {"current": {"state": "inWar"}},
            }
        }

        with patch("elbow_helper.features.wars.state.save_cache"):
            history = manager._load_war_board_history()

        self.assertEqual(history, {"BEH": {"current": valid}})
