from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import discord

from elbow_helper.features.cwl.transfer_hub import CwlTransferHubMixin
from elbow_helper.features.cwl.transfers import CwlTransferMixin
from elbow_helper.features.cwl.views import CwlTransferHubView
from elbow_helper.features.rosters.config import CWL_CLAN_ROSTER_IDS
from elbow_helper.features.rosters.models import LinkedAccount
from elbow_helper.features.rosters.models import RosterMember


class CwlTransferRosterTests(unittest.TestCase):
    def test_server_cwl_roster_ids_are_mapped_by_clan(self) -> None:
        self.assertEqual(
            CWL_CLAN_ROSTER_IDS,
            {
                "BEH": 4,
                "BE4": 5,
                "BES": 6,
                "BE1": 7,
                "BEM": 8,
                "BEC": 9,
                "BEP": 10,
                "BEE": 11,
            },
        )

    def test_current_group_state_confirms_completed_spin(self) -> None:
        self.assertTrue(
            CwlTransferMixin._league_group_confirms_spin(
                {"season": "2026-08", "state": "preparation", "rounds": []},
                "2026-08",
            )
        )

    def test_real_war_tag_confirms_spin_when_group_state_is_unknown(self) -> None:
        self.assertTrue(
            CwlTransferMixin._league_group_confirms_spin(
                {
                    "season": "2026-08",
                    "state": "unknown",
                    "rounds": [{"warTags": ["#WAR"]}],
                },
                "2026-08",
            )
        )

    def test_searching_or_previous_season_group_does_not_confirm_spin(self) -> None:
        self.assertFalse(
            CwlTransferMixin._league_group_confirms_spin(
                {
                    "season": "2026-08",
                    "state": "searching",
                    "rounds": [{"warTags": ["#0"]}],
                },
                "2026-08",
            )
        )
        self.assertFalse(
            CwlTransferMixin._league_group_confirms_spin(
                {
                    "season": "2026-07",
                    "state": "inWar",
                    "rounds": [{"warTags": ["#OLD"]}],
                },
                "2026-08",
            )
        )

    def test_native_roster_mismatches_use_live_account_clans_and_deduplicate_members(self) -> None:
        roster_members = {
            "BEH": [
                RosterMember("#HOME", 10, "Home", "BE4", 18, 1),
                RosterMember("#AWAY", 20, "Away", "BEH", 18, 2),
                RosterMember("#ALT", 20, "Alt", "BEH", 17, 3),
            ]
        }
        profiles = {
            "#HOME": LinkedAccount("#HOME", "Home", "BEH", 18),
            "#AWAY": LinkedAccount("#AWAY", "Away", "BE4", 18),
            "#ALT": LinkedAccount("#ALT", "Alt", "", 17),
        }

        self.assertEqual(
            CwlTransferMixin._native_roster_mismatches(roster_members, profiles),
            {"BEH": [20]},
        )

    def test_transfer_state_discards_obsolete_roster_message_links(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "transfers.json"
            state_path.write_text(
                json.dumps(
                    {
                        "rosters": {"BEH": {"channel_id": 1, "message_id": 2}},
                        "reminder_messages": [],
                    }
                ),
                encoding="utf-8",
            )
            mixin = CwlTransferMixin()
            mixin._transfer_state_needs_save = False

            with patch("elbow_helper.features.cwl.transfers.TRANSFER_STATE_FILE", state_path):
                state = mixin._load_transfer_state()

            self.assertEqual(
                state,
                {
                    "hub_message_id": None,
                    "released_roster_cycles": {},
                    "reminder_messages": [],
                },
            )
            self.assertTrue(mixin._transfer_state_needs_save)

    def test_transfer_state_preserves_released_cycles_across_reboots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "transfers.json"
            expected = {
                "hub_message_id": 123,
                "released_roster_cycles": {"4": 20, "5": 21},
                "reminder_messages": [],
            }
            state_path.write_text(json.dumps(expected), encoding="utf-8")
            mixin = CwlTransferMixin()
            mixin._transfer_state_needs_save = False

            with patch("elbow_helper.features.cwl.transfers.TRANSFER_STATE_FILE", state_path):
                state = mixin._load_transfer_state()

            self.assertEqual(state, expected)
            self.assertFalse(mixin._transfer_state_needs_save)


class CwlTransferHubTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.hub = CwlTransferHubMixin()
        self.beh_member = RosterMember("#BEH", 10, "Ahmad", "BE4", 18, 1)
        self.be4_member = RosterMember("#BE4", 10, "Ahmad v2", "BEH", 17, 2)

    def test_hub_copy_is_short_and_action_focused(self) -> None:
        guild = MagicMock()
        guild.name = "Brown Elbow"
        guild.icon.url = "https://example.com/server-icon.png"
        embed = self.hub._build_transfer_hub_embed(guild, placements_released=True)

        self.assertEqual(embed.title, "CWL Rosters and Transfers")
        self.assertEqual(
            embed.description,
            "See where you’re playing for CWL and whether\n"
            "you still need to move.\n\n"
            "Find your CWL info and war discussion channels,\n"
            "or browse the full rosters.",
        )
        self.assertIsNone(embed.author.name)
        self.assertEqual(embed.thumbnail.url, "https://example.com/server-icon.png")
        self.assertIsNone(embed.footer.text)

    def test_locked_hub_states_that_rosters_have_not_been_announced(self) -> None:
        embed = self.hub._build_transfer_hub_embed(placements_released=False)

        self.assertEqual(
            embed.description,
            "The CWL rosters haven’t been announced yet.",
        )

    def test_hub_message_url_uses_the_saved_message(self) -> None:
        self.hub.transfer_state = {"hub_message_id": 123}

        self.assertEqual(
            self.hub._transfer_hub_url(),
            "https://discord.com/channels/1063032179011096597/1168637963526209546/123",
        )

        self.hub.transfer_state = {"hub_message_id": None}
        self.assertIsNone(self.hub._transfer_hub_url())

    async def test_hub_buttons_use_plain_member_language(self) -> None:
        class Cog:
            @staticmethod
            def _full_rosters_url() -> str:
                return "https://discord.com/channels/1/2"

        view = CwlTransferHubView(Cog(), placements_released=True)
        labels = [item.label for item in view.children]

        self.assertEqual(
            labels,
            ["Where Am I Playing?", "CWL Channels", "See All Rosters"],
        )
        self.assertEqual(view.children[0].style, discord.ButtonStyle.success)
        self.assertEqual(view.children[1].style, discord.ButtonStyle.secondary)
        self.assertEqual(view.children[2].style, discord.ButtonStyle.link)
        self.assertFalse(any(item.disabled for item in view.children))

        locked_view = CwlTransferHubView(Cog(), placements_released=False)
        self.assertTrue(locked_view.children[0].disabled)
        self.assertTrue(locked_view.children[1].disabled)
        self.assertFalse(locked_view.children[2].disabled)

    async def test_release_state_matches_the_current_native_roster_cycles(self) -> None:
        rosters = [
            SimpleNamespace(id=roster_id, clan_code=clan_code, active_cycle_id=roster_id + 20)
            for clan_code, roster_id in CWL_CLAN_ROSTER_IDS.items()
        ]
        queries = MagicMock()
        queries.list_for_guild = AsyncMock(return_value=rosters)
        self.hub.roster_queries = queries
        self.hub.transfer_state = {
            "hub_message_id": 123,
            "released_roster_cycles": {},
            "reminder_messages": [],
        }
        self.hub._save_transfer_state = MagicMock()

        cycles = await self.hub._current_cwl_roster_cycles(1)

        self.assertIsNotNone(cycles)
        self.assertFalse(await self.hub._cwl_placements_release_status(1))
        self.hub._release_cwl_placements(cycles)
        self.assertTrue(await self.hub._cwl_placements_release_status(1))
        self.hub._save_transfer_state.assert_called_once_with()

        rosters[0].active_cycle_id += 1
        self.assertFalse(await self.hub._cwl_placements_release_status(1))

    async def test_new_mapped_roster_cycle_refreshes_the_hub(self) -> None:
        self.hub.transfer_state = {
            "released_roster_cycles": {"4": 10},
        }
        self.hub.ensure_transfer_hub = AsyncMock(return_value=True)
        roster = SimpleNamespace(id=4, active_cycle_id=11)

        await self.hub.on_roster_cycle_opened(roster)

        self.hub.ensure_transfer_hub.assert_awaited_once_with()

    async def test_unreleased_account_lookup_stops_before_reading_assignments(self) -> None:
        self.hub._cwl_placements_release_status = AsyncMock(return_value=False)
        self.hub._member_cwl_assignments = AsyncMock()
        interaction = MagicMock()
        interaction.guild_id = 1
        interaction.response.send_message = AsyncMock()

        await self.hub.show_member_cwl(interaction)

        interaction.response.send_message.assert_awaited_once_with(
            "The CWL rosters haven’t been announced yet.",
            ephemeral=True,
        )
        self.hub._member_cwl_assignments.assert_not_awaited()

    def test_account_result_shows_live_location_and_destination(self) -> None:
        embed = self.hub._build_member_cwl_embed(
            {"BEH": [self.beh_member], "BE4": [self.be4_member]},
            {
                "#BEH": LinkedAccount("#BEH", "Ahmad", "BEH", 18),
                "#BE4": LinkedAccount("#BE4", "Ahmad v2", "BES", 17),
            },
            set(),
        )

        self.assertEqual(embed.title, "Where You’re Playing")
        self.assertEqual(
            embed.description,
            "1 of your 2 accounts still needs to move.\n\n"
            "[Browse every CWL roster](https://discord.com/channels/1063032179011096597/1257094469757567066)",
        )
        self.assertEqual(
            embed.fields[0].name,
            "CWL clan",
        )
        self.assertTrue(
            embed.fields[0].value.startswith(
                "[Hellbow (BEH) · #2Y2PJCVGU](http://cprk.us/c/2Y2PJCVGU)\n\n"
            )
        )
        self.assertIn("Currently in BEH", embed.fields[0].value)
        self.assertNotIn("No move needed", embed.fields[0].value)
        self.assertIn("Currently in BES · Move to BE4", embed.fields[1].value)

    def test_single_ready_account_result_avoids_a_redundant_summary(self) -> None:
        embed = self.hub._build_member_cwl_embed(
            {"BEH": [self.beh_member]},
            {"#BEH": LinkedAccount("#BEH", "Ahmad", "BEH", 18)},
            set(),
        )

        self.assertEqual(
            embed.description,
            "[Browse every CWL roster](https://discord.com/channels/1063032179011096597/1257094469757567066)",
        )

    def test_channel_result_uses_registered_routes_and_omits_missing_bep_war_channel(self) -> None:
        embed = self.hub._build_member_channels_embed(
            {
                "BE4": [self.be4_member],
                "BEP": [RosterMember("#BEP", 10, "Third", "BEP", 16, 3)],
            }
        )
        fields = {field.name: field.value for field in embed.fields}

        self.assertEqual(embed.title, "Channels for Your CWL Clans")
        self.assertIn("CWL info: <#1134604210386767982>", fields["BE4"])
        self.assertIn("War discussion: <#1136389893661134960>", fields["BE4"])
        self.assertEqual(fields["BEP"], "CWL info: <#1324125900739973232>")


if __name__ == "__main__":
    unittest.main()
