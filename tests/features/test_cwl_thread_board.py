from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import discord

from elbow_helper.configuration.clans import CLAN_NAMES
from elbow_helper.configuration.clans import CLAN_TAGS
from elbow_helper.features.cwl.threads.board import CwlThreadBoardMixin
from elbow_helper.features.cwl.threads.emojis import EMPTY_CWL_THREAD_EMOJIS
from elbow_helper.features.cwl.threads.snapshots import CwlThreadRound
from elbow_helper.features.cwl.threads.snapshots import CwlThreadSnapshot
from elbow_helper.features.cwl.threads.snapshots import build_cwl_thread_snapshot
from elbow_helper.features.cwl.threads.snapshots import cwl_thread_snapshot_is_complete


NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


def _war(
    state: str,
    round_number: int,
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    attacks: int = 0,
    total_rounds: int = 7,
    stale: bool = False,
) -> dict[str, object]:
    start_at = start_at or NOW - timedelta(hours=1)
    end_at = end_at or NOW + timedelta(hours=1)
    members = [
        {
            "name": "Player One",
            "attacks": [{"stars": 3}] if attacks >= 1 else [],
        },
        {
            "name": "Player Two",
            "attacks": [{"stars": 2}] if attacks >= 2 else [],
        },
    ]
    return {
        "state": state,
        "_state": state,
        "_round": round_number,
        "_total_rounds": total_rounds,
        "_season": "2026-09",
        "_warTag": f"#WAR{round_number}",
        "_snapshot_stale": stale,
        "teamSize": 2,
        "attacksPerMember": 1,
        "startTime": start_at.strftime("%Y%m%dT%H%M%S.000Z"),
        "endTime": end_at.strftime("%Y%m%dT%H%M%S.000Z"),
        "clan": {
            "name": "Hellbow",
            "tag": CLAN_TAGS["BEH"],
            "badgeUrls": {"small": "https://example.com/beh.png"},
            "attacks": attacks,
            "members": members,
        },
        "opponent": {
            "name": f"Rival {round_number}",
            "tag": f"#RIVAL{round_number}",
            "members": [],
        },
    }


def _round(
    state: str,
    round_number: int,
    *,
    war_tag: str | None = None,
) -> CwlThreadRound:
    return CwlThreadRound(
        round_number=round_number,
        war_tag=war_tag or f"#WAR{round_number}",
        season="2026-09",
        clan_name="Hellbow",
        clan_tag=CLAN_TAGS["BEH"],
        clan_badge_url="https://example.com/beh.png",
        opponent_name=f"Rival {round_number}",
        opponent_tag=f"#RIVAL{round_number}",
        state=state,
        start_at=NOW + timedelta(hours=8),
        end_at=NOW + timedelta(hours=1),
        attacks_used=27,
        attacks_total=30,
        missing_attacks=("Player One", "Player Two") if state == "inwar" else (),
        is_stale=False,
    )


class ThreadBoardHarness(CwlThreadBoardMixin):
    def __init__(self) -> None:
        self._thread_snapshot_cache = {}
        self._sticky_last_repost_at = {}
        self._sticky_update_locks = {}
        self.clan_configs = {
            CLAN_NAMES["BEH"]: {"thread_id": 123},
        }
        self.data = {
            "threads": {
                "123": {
                    "clan_name": CLAN_NAMES["BEH"],
                    "sticky_message_id": 900,
                    "cc_status": {},
                    "cc_statuses": {},
                }
            }
        }
        self.account_links = SimpleNamespace(get_clan_badge_url=lambda _: None)
        self.cwl_thread_emojis = SimpleNamespace(
            get=AsyncMock(return_value=EMPTY_CWL_THREAD_EMOJIS)
        )
        self.save_data = MagicMock()
        self.check_permissions = MagicMock(return_value=True)

    @staticmethod
    def _utc_now() -> datetime:
        return NOW

    @staticmethod
    def _utc_now_iso() -> str:
        return NOW.isoformat()

    def _get_sticky_lock(self, thread_id: str):
        import asyncio

        return self._sticky_update_locks.setdefault(thread_id, asyncio.Lock())


class CwlThreadSnapshotTests(unittest.TestCase):
    def test_snapshot_keeps_battle_and_next_preparation_in_round_order(self) -> None:
        wars = [
            _war("warEnded", 2),
            _war("inWar", 3, attacks=1),
            _war("preparation", 4, start_at=NOW + timedelta(hours=8)),
        ]

        snapshot = build_cwl_thread_snapshot(
            wars,
            CLAN_TAGS["BEH"],
            now=NOW,
        )

        self.assertEqual(snapshot.battle.round_number, 3)
        self.assertEqual(snapshot.preparation.round_number, 4)
        self.assertEqual(snapshot.battle.attacks_used, 1)
        self.assertEqual(snapshot.battle.attacks_total, 2)
        self.assertEqual(snapshot.battle.missing_attacks, ("Player Two",))

    def test_future_inwar_payload_is_treated_as_preparation(self) -> None:
        wars = [_war("inWar", 1, start_at=NOW + timedelta(hours=4))]

        snapshot = build_cwl_thread_snapshot(
            wars,
            CLAN_TAGS["BEH"],
            now=NOW,
        )

        self.assertIsNone(snapshot.battle)
        self.assertEqual(snapshot.preparation.round_number, 1)

    def test_partial_overlap_is_not_safe_for_the_thread_board(self) -> None:
        battle_only = [_war("inWar", 3)]

        self.assertFalse(
            cwl_thread_snapshot_is_complete(battle_only, now=NOW)
        )

    def test_prep_remains_safe_after_the_previous_battle_ends(self) -> None:
        wars = [
            _war("warEnded", 3),
            _war("preparation", 4, start_at=NOW + timedelta(hours=8)),
        ]

        self.assertTrue(cwl_thread_snapshot_is_complete(wars, now=NOW))
        snapshot = build_cwl_thread_snapshot(
            wars,
            CLAN_TAGS["BEH"],
            now=NOW,
        )
        self.assertIsNone(snapshot.battle)
        self.assertEqual(snapshot.preparation.round_number, 4)

    def test_final_ended_round_is_a_complete_inactive_snapshot(self) -> None:
        wars = [_war("warEnded", round_number) for round_number in range(1, 8)]

        self.assertTrue(cwl_thread_snapshot_is_complete(wars, now=NOW))
        snapshot = build_cwl_thread_snapshot(
            wars,
            CLAN_TAGS["BEH"],
            now=NOW,
        )
        self.assertFalse(snapshot.has_active_round)

    def test_ended_snapshot_without_round_total_cannot_remove_the_board(self) -> None:
        wars = [_war("warEnded", 7)]
        wars[0].pop("_total_rounds")

        self.assertFalse(cwl_thread_snapshot_is_complete(wars, now=NOW))

    def test_active_snapshot_without_round_total_cannot_hide_a_section(self) -> None:
        wars = [_war("inWar", 3)]
        wars[0].pop("_total_rounds")

        self.assertFalse(cwl_thread_snapshot_is_complete(wars, now=NOW))


class CwlThreadRenderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_compact_board_keeps_battle_and_preparation_context_separate(self) -> None:
        manager = ThreadBoardHarness()
        snapshot = CwlThreadSnapshot(
            battle=_round("inwar", 3),
            preparation=_round("preparation", 4),
        )

        embed, view = await manager._build_thread_status_board(
            "BEH",
            snapshot,
            "empty",
        )

        self.assertEqual(embed.title, "CWL Status")
        self.assertEqual(embed.author.name, f"Hellbow • {CLAN_TAGS['BEH']}")
        self.assertEqual(embed.author.icon_url, "https://example.com/beh.png")
        self.assertEqual(len(embed.fields), 2)
        self.assertIn("Day 3 · Battle vs Rival 3", embed.fields[0].name)
        self.assertIn("27/30 attacks", embed.fields[0].value)
        self.assertIn("Missing: Player One, Player Two", embed.fields[0].value)
        self.assertIn("Day 4 · Preparation vs Rival 4", embed.fields[1].name)
        self.assertIn("CCs empty", embed.fields[1].value)
        self.assertNotIn("Day 1", str(embed.to_dict()))
        self.assertIsNotNone(view)
        self.assertEqual([item.label for item in view.children], ["Filled", "Partial", "Empty"])
        self.assertEqual(
            [item.custom_id for item in view.children],
            [
                "cwl:cc_status:beh:filled",
                "cwl:cc_status:beh:partial",
                "cwl:cc_status:beh:empty",
            ],
        )
        self.assertEqual(
            [item.disabled for item in view.children],
            [False, False, True],
        )

    async def test_final_battle_hides_all_cc_information_and_controls(self) -> None:
        manager = ThreadBoardHarness()
        snapshot = CwlThreadSnapshot(
            battle=_round("inwar", 7),
            preparation=None,
        )

        embed, view = await manager._build_thread_status_board(
            "BEH",
            snapshot,
            None,
        )

        self.assertEqual(len(embed.fields), 1)
        self.assertIn("Battle", embed.fields[0].name)
        self.assertNotIn("CC", str(embed.to_dict()))
        self.assertIsNone(view)

    def test_legacy_day_status_migrates_to_the_actual_prep_war(self) -> None:
        manager = ThreadBoardHarness()
        thread_data = manager.data["threads"]["123"]
        thread_data["cc_status"] = {"4": "partial"}
        thread_data["cc_statuses"] = {
            "#WAR3": {"round": 3, "season": "2026-09", "status": "filled"}
        }
        preparation = _round("preparation", 4)

        status, changed = manager._prepare_cc_state(
            thread_data,
            preparation,
        )

        self.assertTrue(changed)
        self.assertEqual(status, "partial")
        self.assertEqual(thread_data["cc_status"], {})
        self.assertEqual(
            thread_data["cc_statuses"]["#WAR4"]["status"],
            "partial",
        )
        self.assertNotIn("#WAR3", thread_data["cc_statuses"])

    def test_cc_state_is_cleared_when_no_preparation_is_active(self) -> None:
        manager = ThreadBoardHarness()
        thread_data = manager.data["threads"]["123"]
        thread_data["cc_statuses"] = {
            "#WAR7": {"round": 7, "season": "2026-09", "status": "filled"}
        }
        thread_data["active_prep"] = {
            "war_tag": "#WAR7",
            "round": 7,
            "season": "2026-09",
        }

        status, changed = manager._prepare_cc_state(thread_data, None)

        self.assertIsNone(status)
        self.assertTrue(changed)
        self.assertEqual(thread_data["cc_statuses"], {})
        self.assertNotIn("active_prep", thread_data)


class CwlThreadSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_board_is_not_edited_but_status_change_is(self) -> None:
        manager = ThreadBoardHarness()
        snapshot = CwlThreadSnapshot(
            battle=_round("inwar", 3),
            preparation=_round("preparation", 4),
        )
        existing_embed, existing_view = await manager._build_thread_status_board(
            "BEH",
            snapshot,
            "empty",
        )
        existing_message = SimpleNamespace(
            id=900,
            embeds=[existing_embed],
            components=[
                SimpleNamespace(to_dict=lambda payload=payload: payload)
                for payload in existing_view.to_components()
            ],
            edit=AsyncMock(),
        )
        thread = MagicMock(spec=discord.Thread)
        thread.id = 123
        thread.fetch_message = AsyncMock(return_value=existing_message)
        thread.send = AsyncMock()

        async def run_operation(thread_arg, operation, coro_factory):
            return True, await coro_factory()

        manager._run_sticky_http_operation = AsyncMock(side_effect=run_operation)
        manager._cleanup_stale_sticky_messages = AsyncMock(return_value=False)
        thread_data = manager.data["threads"]["123"]

        unchanged = await manager._sync_thread_status_board(
            "BEH",
            thread,
            thread_data,
            snapshot,
        )

        self.assertTrue(unchanged)
        existing_message.edit.assert_not_awaited()
        thread.send.assert_not_awaited()

        thread_data["cc_statuses"]["#WAR4"]["status"] = "filled"
        changed = await manager._sync_thread_status_board(
            "BEH",
            thread,
            thread_data,
            snapshot,
        )

        self.assertTrue(changed)
        existing_message.edit.assert_awaited_once()
        thread.send.assert_not_awaited()


class CwlThreadLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_or_stale_data_keeps_the_existing_board(self) -> None:
        manager = ThreadBoardHarness()
        manager._remove_thread_status_board = AsyncMock()
        manager._resolve_registered_thread = AsyncMock()

        missing = await manager.sync_registered_cwl_thread("BEH", [])
        stale = await manager.sync_registered_cwl_thread(
            "BEH",
            [_war("preparation", 1, start_at=NOW + timedelta(hours=4), stale=True)],
        )
        unidentified_wars = [
            _war("preparation", 1, start_at=NOW + timedelta(hours=4))
        ]
        unidentified_wars[0].pop("_season")
        unidentified = await manager.sync_registered_cwl_thread(
            "BEH",
            unidentified_wars,
        )

        self.assertFalse(missing)
        self.assertFalse(stale)
        self.assertFalse(unidentified)
        manager._remove_thread_status_board.assert_not_awaited()
        manager._resolve_registered_thread.assert_not_awaited()

    async def test_complete_final_snapshot_removes_the_board(self) -> None:
        manager = ThreadBoardHarness()
        manager._remove_thread_status_board = AsyncMock(return_value=True)
        wars = [_war("warEnded", round_number) for round_number in range(1, 8)]

        synced = await manager.sync_registered_cwl_thread("BEH", wars)

        self.assertTrue(synced)
        manager._remove_thread_status_board.assert_awaited_once()
        thread_data = manager.data["threads"]["123"]
        self.assertEqual(thread_data["cc_statuses"], {})
        self.assertNotIn("active_prep", thread_data)

    async def test_two_human_messages_do_not_repost_the_board(self) -> None:
        manager = ThreadBoardHarness()
        manager._repost_existing_thread_status = AsyncMock(return_value=True)
        thread = MagicMock(spec=discord.Thread)
        thread.id = 123

        async def history(**kwargs):
            for message_id in (902, 901):
                yield SimpleNamespace(
                    id=message_id,
                    author=SimpleNamespace(bot=False),
                )

        thread.history = history
        with patch(
            "elbow_helper.features.cwl.threads.board.time.monotonic",
            return_value=60.0,
        ):
            reposted = await manager._repost_thread_status_from_activity(thread)

        self.assertFalse(reposted)
        manager._repost_existing_thread_status.assert_not_awaited()

    async def test_third_human_message_reposts_immediately_without_a_timer(self) -> None:
        manager = ThreadBoardHarness()
        manager._repost_existing_thread_status = AsyncMock(return_value=True)
        thread = MagicMock(spec=discord.Thread)
        thread.id = 123

        async def history(**kwargs):
            yield SimpleNamespace(id=904, author=SimpleNamespace(bot=False))
            yield SimpleNamespace(id=903, author=SimpleNamespace(bot=True))
            yield SimpleNamespace(id=902, author=SimpleNamespace(bot=False))
            yield SimpleNamespace(id=901, author=SimpleNamespace(bot=False))

        thread.history = history
        with patch(
            "elbow_helper.features.cwl.threads.board.time.monotonic",
            return_value=60.0,
        ):
            reposted = await manager._repost_thread_status_from_activity(thread)

        self.assertTrue(reposted)
        manager._repost_existing_thread_status.assert_awaited_once()

    async def test_repost_cooldown_blocks_repeated_bumps(self) -> None:
        manager = ThreadBoardHarness()
        manager._sticky_last_repost_at["123"] = 950.0
        manager._repost_existing_thread_status = AsyncMock(return_value=True)
        thread = MagicMock(spec=discord.Thread)
        thread.id = 123
        thread.history = MagicMock()
        with patch(
            "elbow_helper.features.cwl.threads.board.time.monotonic",
            return_value=1000.0,
        ):
            reposted = await manager._repost_thread_status_from_activity(thread)

        self.assertFalse(reposted)
        thread.history.assert_not_called()


class CwlThreadButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_button_records_the_current_prep_war_and_refreshes_in_place(self) -> None:
        manager = ThreadBoardHarness()
        snapshot = CwlThreadSnapshot(
            battle=_round("inwar", 3),
            preparation=_round("preparation", 4),
        )
        wars = [
            _war("inWar", 3, attacks=1),
            _war("preparation", 4, start_at=NOW + timedelta(hours=8)),
        ]
        manager._latest_thread_snapshot = AsyncMock(return_value=(wars, snapshot))
        manager.sync_registered_cwl_thread = AsyncMock(return_value=True)

        channel = MagicMock(spec=discord.Thread)
        channel.id = 123
        interaction = MagicMock(spec=discord.Interaction)
        interaction.channel = channel
        interaction.message = SimpleNamespace(id=900)
        interaction.user = SimpleNamespace(id=42)
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        await manager.update_cc_status_from_button(interaction, "BEH", "filled")

        record = manager.data["threads"]["123"]["cc_statuses"]["#WAR4"]
        self.assertEqual(record["status"], "filled")
        self.assertEqual(record["round"], 4)
        self.assertEqual(record["season"], "2026-09")
        self.assertEqual(record["updated_by"], 42)
        manager._latest_thread_snapshot.assert_awaited_once_with(
            "BEH",
            force_refresh=True,
        )
        manager.sync_registered_cwl_thread.assert_awaited_once_with("BEH", wars)
        interaction.followup.send.assert_awaited_once_with(
            "Day 4 CCs marked **filled**.",
            ephemeral=True,
        )

    async def test_old_duplicate_board_cannot_change_status(self) -> None:
        manager = ThreadBoardHarness()
        manager._latest_thread_snapshot = AsyncMock()

        channel = MagicMock(spec=discord.Thread)
        channel.id = 123
        interaction = MagicMock(spec=discord.Interaction)
        interaction.channel = channel
        interaction.message = SimpleNamespace(id=899)
        interaction.user = SimpleNamespace(id=42)
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()

        await manager.update_cc_status_from_button(interaction, "BEH", "filled")

        manager._latest_thread_snapshot.assert_not_awaited()
        self.assertEqual(manager.data["threads"]["123"]["cc_statuses"], {})
        interaction.response.send_message.assert_awaited_once_with(
            "This is an older CWL status post. Use the latest one.",
            ephemeral=True,
        )



if __name__ == "__main__":
    unittest.main()
