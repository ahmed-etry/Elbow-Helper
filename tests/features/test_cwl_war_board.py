from __future__ import annotations

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import discord

from elbow_helper.features.cwl.war_board import CwlWarBoardMixin
from elbow_helper.features.cwl.war_board import cwl_board_snapshot_is_complete
from elbow_helper.features.cwl.war_board import orient_cwl_war
from elbow_helper.features.cwl.war_board import select_cwl_board_wars
from elbow_helper.features.cwl.router import CwlRouterMixin
from elbow_helper.features.wars.emojis import EMPTY_WAR_EMOJIS
from elbow_helper.configuration.channels import CLAN_CWL_INFO_CHANNELS
from elbow_helper.configuration.clans import CLAN_TAGS


def _cwl_war(
    state: str,
    round_number: int,
    *,
    own_side: str = "clan",
) -> dict[str, object]:
    own = {
        "tag": CLAN_TAGS["BEH"],
        "name": "Hellbow",
        "badgeUrls": {"small": "https://example.com/beh.png"},
        "stars": round_number,
        "attacks": round_number,
        "destructionPercentage": 50.0,
        "members": [
            {
                "tag": "#P2",
                "name": "Player",
                "townhallLevel": 18,
                "mapPosition": 1,
                "attacks": [],
            }
        ],
    }
    opponent = {
        "tag": f"#RIVAL{round_number}",
        "name": f"Rival {round_number}",
        "badgeUrls": {"small": "https://example.com/rival.png"},
        "stars": 0,
        "attacks": 0,
        "destructionPercentage": 0.0,
        "members": [
            {
                "tag": "#Q2",
                "name": "Opponent",
                "townhallLevel": 18,
                "mapPosition": 1,
                "attacks": [],
            }
        ],
    }
    return {
        "state": state,
        "_state": state,
        "_round": round_number,
        "_warTag": f"#WAR{round_number}",
        "teamSize": 1,
        "attacksPerMember": 1,
        "startTime": f"2026072{round_number}T210000.000Z",
        "endTime": f"2026072{round_number + 1}T210000.000Z",
        "clan": own if own_side == "clan" else opponent,
        "opponent": opponent if own_side == "clan" else own,
    }


class CwlWarSelectionTests(unittest.TestCase):
    def test_back_to_back_days_select_battle_then_preparation(self) -> None:
        wars = [
            _cwl_war("warEnded", 1),
            _cwl_war("inWar", 2),
            _cwl_war("preparation", 3),
        ]

        selected = select_cwl_board_wars(wars)

        self.assertEqual([war["_round"] for war in selected], [2, 3])

    def test_final_day_keeps_only_the_latest_result(self) -> None:
        wars = [_cwl_war("warEnded", round_number) for round_number in (1, 7, 3)]

        selected = select_cwl_board_wars(wars)

        self.assertEqual([war["_round"] for war in selected], [7])

    def test_configured_clan_is_oriented_consistently(self) -> None:
        payload = _cwl_war("inWar", 2, own_side="opponent")

        oriented = orient_cwl_war(payload, CLAN_TAGS["BEH"])

        self.assertIsNotNone(oriented)
        self.assertEqual(oriented["clan"]["tag"], CLAN_TAGS["BEH"])
        self.assertNotEqual(payload["clan"]["tag"], CLAN_TAGS["BEH"])

    def test_partial_overlap_snapshot_is_rejected(self) -> None:
        battle = _cwl_war("inWar", 2)
        battle["_total_rounds"] = 7
        battle_and_prep = [battle, _cwl_war("preparation", 3)]
        battle_and_prep[1]["_total_rounds"] = 7

        self.assertFalse(cwl_board_snapshot_is_complete([battle]))
        self.assertTrue(cwl_board_snapshot_is_complete(battle_and_prep))


class CwlWarBoardLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_registered_message_contains_battle_and_preparation(self) -> None:
        manager = CwlWarBoardMixin()
        manager.cwl_board_registry = {}
        manager.state = {"cwl_board_messages": manager.cwl_board_registry}
        manager._cwl_board_locks = {}
        manager._save_state = AsyncMock()
        manager.cwl_war_emojis = MagicMock()
        manager.cwl_war_emojis.get = AsyncMock(return_value=EMPTY_WAR_EMOJIS)

        channel = MagicMock(spec=discord.TextChannel)
        channel.id = CLAN_CWL_INFO_CHANNELS["BEH"]
        posted = MagicMock()
        posted.id = 900
        posted.channel = channel
        posted.edit = AsyncMock()
        channel.send = AsyncMock(return_value=posted)

        synced = await manager._upsert_cwl_board(
            "BEH",
            channel,
            [_cwl_war("inWar", 2), _cwl_war("preparation", 3)],
        )

        self.assertTrue(synced)
        channel.send.assert_awaited_once()
        embeds = channel.send.await_args.kwargs["embeds"]
        self.assertEqual(len(embeds), 2)
        self.assertIn("Battle Day", embeds[0].description)
        self.assertIn("Preparation Day", embeds[1].description)
        self.assertEqual(
            manager.cwl_board_registry["BEH"],
            {"channel": channel.id, "message": 900},
        )

        posted.embeds = embeds
        posted.components = []
        channel.fetch_message = AsyncMock(return_value=posted)
        await manager._upsert_cwl_board(
            "BEH",
            channel,
            [_cwl_war("inWar", 2), _cwl_war("preparation", 3)],
        )

        channel.send.assert_awaited_once()
        posted.edit.assert_not_awaited()

    async def test_missing_war_data_leaves_the_registered_board_untouched(self) -> None:
        manager = CwlWarBoardMixin()
        manager.cwl_board_registry = {
            "BEH": {"channel": CLAN_CWL_INFO_CHANNELS["BEH"], "message": 900}
        }
        manager.state = {"cwl_board_messages": manager.cwl_board_registry}
        manager._cwl_board_locks = {}
        manager._save_state = AsyncMock()
        manager.cwl_war_emojis = MagicMock()
        manager.cwl_war_emojis.get = AsyncMock()
        channel = MagicMock(spec=discord.TextChannel)
        channel.id = CLAN_CWL_INFO_CHANNELS["BEH"]
        channel.send = AsyncMock()
        channel.fetch_message = AsyncMock()

        synced = await manager._upsert_cwl_board("BEH", channel, [])

        self.assertFalse(synced)
        channel.fetch_message.assert_not_awaited()
        channel.send.assert_not_awaited()
        manager.cwl_war_emojis.get.assert_not_awaited()

    async def test_channel_sync_updates_only_the_war_board(self) -> None:
        manager = CwlWarBoardMixin()
        channel = MagicMock(spec=discord.TextChannel)
        manager._cwl_board_channel = AsyncMock(return_value=channel)
        manager._upsert_cwl_board = AsyncMock(return_value=True)
        wars = [_cwl_war("preparation", 1)]

        await manager._sync_cwl_channel("BEH", wars)

        manager._upsert_cwl_board.assert_awaited_once_with("BEH", channel, wars)


class CwlWarBoardStateTests(unittest.TestCase):
    def test_registered_board_restores_after_a_reboot(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        state_path = Path(temp_dir.name) / "router.json"
        state_path.write_text(
            json.dumps(
                {
                    "cwl_board_messages": {
                        "BEH": {
                            "channel": CLAN_CWL_INFO_CHANNELS["BEH"],
                            "message": 900,
                        },
                        "invalid": {"channel": "bad", "message": None},
                    }
                }
            ),
            encoding="utf-8",
        )
        manager = CwlRouterMixin()

        with patch("elbow_helper.features.cwl.router.ROUTER_STATE_FILE", state_path):
            state = manager._load_state()

        self.assertEqual(
            state["cwl_board_messages"],
            {
                "BEH": {
                    "channel": CLAN_CWL_INFO_CHANNELS["BEH"],
                    "message": 900,
                }
            },
        )


class CwlRotationSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_rotation_roster_preserves_the_previous_baseline(self) -> None:
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                current = cls(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
                return current if tz is not None else current.replace(tzinfo=None)

        war = _cwl_war("preparation", 1)
        war["teamSize"] = 2
        war["_start_dt"] = FixedDatetime(2026, 9, 2, tzinfo=timezone.utc)

        manager = CwlRouterMixin()
        manager.state = {
            "rosters": {"BEH": {"#WAR1": ["#P2", "#P3"]}},
            "roster_names": {
                "BEH": {"#WAR1": {"#P2": "Player", "#P3": "Other"}}
            },
            "last_war_tag": {"BEH": "#WAR1"},
            "missed_posted": {},
            "last_poll_ts": 0,
            "name_cache": {},
        }
        manager._get_league_wars = AsyncMock(return_value=[war])
        manager._sync_cwl_channel = AsyncMock()
        manager._log_rotation_api = AsyncMock()
        manager._save_state = AsyncMock()

        with (
            patch(
                "elbow_helper.features.cwl.router.CWL_CLAN_TAGS",
                {"BEH": CLAN_TAGS["BEH"]},
            ),
            patch("elbow_helper.features.cwl.router.datetime", FixedDatetime),
            self.assertLogs(
                "elbow_helper.features.cwl.router",
                level="WARNING",
            ),
        ):
            await manager._poll_once()

        self.assertEqual(
            manager.state["rosters"]["BEH"]["#WAR1"],
            ["#P2", "#P3"],
        )
        manager._log_rotation_api.assert_not_awaited()

if __name__ == "__main__":
    unittest.main()
