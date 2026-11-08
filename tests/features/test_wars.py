from __future__ import annotations

import asyncio
import unittest
from copy import deepcopy
from datetime import datetime
from datetime import timezone
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch


from elbow_helper.infrastructure.discord.publishing import PublishingQueue
from elbow_helper.features.wars.board import WarBoardMixin
from elbow_helper.features.wars.board import PreviousWarView
from elbow_helper.infrastructure.discord.war_emojis import WarEmojiSet
from elbow_helper.features.wars.config import NOTICE_TTL
from elbow_helper.features.wars.helpers import HelperMixin
from elbow_helper.features.wars.record import RecordMixin
from elbow_helper.infrastructure.discord.war_boards import build_war_board_embed
from elbow_helper.infrastructure.discord.war_boards import build_war_summary_embed
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

    manager.bot = MagicMock(publishing=MagicMock(wraps=PublishingQueue()))
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


async def _messages(messages):
    for message in messages:
        yield message


class _WarSummaryManager(RecordMixin, WarflowMixin, HelperMixin, StateMixin, TaskMixin):
    pass


def _war_summary_manager() -> tuple[_WarSummaryManager, MagicMock, MagicMock]:
    manager = _WarSummaryManager()
    # The record does not hold this war; misses are named from the lineup.
    manager.war_reads = MagicMock(completed=AsyncMock(return_value={"rows": []}))
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

    manager.bot = MagicMock(publishing=MagicMock(wraps=PublishingQueue()))
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


class WarBoardLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_wars_edit_the_registered_message_instead_of_posting_again(self) -> None:
        manager = WarBoardMixin()
        manager.record_boards = {}
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
        manager.bot = MagicMock(publishing=MagicMock(wraps=PublishingQueue()))
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
        manager.bot.publishing.edit_message.assert_called_once()
        posted.edit.assert_awaited_once()
        self.assertEqual(
            manager.war_board_registry["BEH"],
            {"channel": channel.id, "message": posted.id},
        )


    async def test_next_war_adds_access_to_the_completed_previous_war(self) -> None:
        manager = WarBoardMixin()
        manager.war_board_registry = {}
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
        manager.bot = MagicMock(publishing=MagicMock(wraps=PublishingQueue()))
        manager.bot.get_channel.return_value = channel

        ended = _war_payload("warEnded")
        manager.record_boards = {"BEH": {"current": ended}}
        await manager._update_war_board("Hellbow", ended)
        self.assertIsNone(channel.send.await_args.kwargs["view"])

        posted.embeds = [build_war_board_embed(ended, _war_emojis())]
        posted.components = []
        upcoming = _war_payload("preparation")
        upcoming["preparationStartTime"] = "20260722T210000.000Z"
        upcoming["startTime"] = "20260723T210000.000Z"
        upcoming["endTime"] = "20260724T210000.000Z"
        upcoming["opponent"]["tag"] = "#NEXT"
        upcoming["opponent"]["name"] = "Next Rival"
        # The record refresh keeps the ended war as the board's previous war.
        manager.record_boards = {"BEH": {"current": upcoming, "previous": ended}}

        await manager._update_war_board("Hellbow", upcoming)

        self.assertIsInstance(posted.edit.await_args.kwargs["view"], PreviousWarView)


    async def test_previous_war_button_is_restored_after_reboot(self) -> None:
        manager = WarBoardMixin()
        manager.war_board_registry = {
            "BEH": {"channel": CLAN_WAR_CHANNELS["BEH"], "message": 900}
        }
        manager.record_boards = {
            "BEH": {
                "current": _war_payload("preparation"),
                "previous": _war_payload("warEnded"),
            }
        }
        manager.bot = MagicMock(publishing=MagicMock(wraps=PublishingQueue()))

        manager._register_war_board_view("BEH")
        manager._register_war_board_view("BE4")

        manager.bot.add_view.assert_called_once()
        view = manager.bot.add_view.call_args.args[0]
        self.assertIsInstance(view, PreviousWarView)
        self.assertEqual(manager.bot.add_view.call_args.kwargs["message_id"], 900)
