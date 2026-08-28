from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import discord

from elbow_helper.features.achievements.raffle import AchievementRaffleMixin
from elbow_helper.features.cwl.bonus.commands import CwlBonusMixin
from elbow_helper.features.cwl.bonus.config import BonusConfigRepository
from elbow_helper.features.event_stats.cog import EventStatsCog


class ApprovedCopyTests(unittest.IsolatedAsyncioTestCase):
    async def test_raffle_clear_confirmations_name_only_what_was_cleared(self) -> None:
        owner = object.__new__(AchievementRaffleMixin)
        owner._month_key = MagicMock(return_value=202607)
        owner._set_raffle_hub_state_internal = AsyncMock()
        cursor = MagicMock()

        winners_only = await owner._raffle_clear_internal(
            cursor,
            clear_tickets=False,
        )
        winners_and_tickets = await owner._raffle_clear_internal(
            cursor,
            clear_tickets=True,
        )

        self.assertEqual(winners_only, "Cleared this month's raffle winners.")
        self.assertEqual(
            winners_and_tickets,
            "Cleared this month's raffle winners and tickets.",
        )

    async def test_cwl_bonus_fallback_keeps_the_warning_in_the_final_reply(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workbook = Path(temp_dir) / "bonus.xlsx"
            workbook.write_bytes(b"xlsx")
            report = SimpleNamespace(
                scope_label="BEH",
                season="2026-07",
                eligible_count=12,
                ineligible_count=3,
                attack_count=45,
                warnings=[],
                google_warning="Google Sheets hasn't been set up.",
                google_link=None,
                workbook_path=workbook,
                workbook_name=workbook.name,
                selected_clans=("BEH",),
            )
            message = MagicMock(
                attachments=[SimpleNamespace(url="https://discord.test/bonus.xlsx")]
            )
            message.edit = AsyncMock()
            interaction = MagicMock()
            interaction.followup.send = AsyncMock(return_value=message)

            owner = object.__new__(CwlBonusMixin)
            await owner._send_bonus_report(interaction, report)
            interaction.followup.send.await_args.kwargs["file"].close()

        final_content = message.edit.await_args.kwargs["content"]
        self.assertIn(
            "Google Sheets hasn't been set up. The Excel file is ready to download.",
            final_content,
        )
        self.assertIn("Eligible players: 12", final_content)

    def test_cwl_bonus_setup_errors_do_not_expose_file_details(self) -> None:
        repository = BonusConfigRepository()
        repository.ensure = MagicMock()

        with (
            patch(
                "elbow_helper.features.cwl.bonus.config.read_json",
                side_effect=OSError("private path"),
            ),
            patch("elbow_helper.features.cwl.bonus.config.LOGGER.exception"),
        ):
            config, errors = repository.load()

        self.assertIsNone(config)
        self.assertEqual(errors, ["CWL bonus settings aren't available."])
        self.assertNotIn("private path", errors[0])
        self.assertEqual(
            repository._validate_bonus_config([]),
            ["CWL bonus settings couldn't be read."],
        )
        self.assertEqual(
            repository._validate_bonus_clan_config("BEH", []),
            ["BEH scoring settings couldn't be read."],
        )

    def test_generic_failure_filler_is_not_used_for_reward_delivery(self) -> None:
        source = (
            Path(__file__).parents[2]
            / "elbow_helper"
            / "features"
            / "cwl"
            / "bonus"
            / "dashboard.py"
        ).read_text(encoding="utf-8-sig")

        self.assertNotIn("Something went wrong while sending the rewards.", source)
        self.assertIn(
            "The rewards couldn't be sent, so the clan was left for review.",
            source,
        )

    async def test_event_channel_failure_hides_discord_exception_details(self) -> None:
        owner = object.__new__(EventStatsCog)
        event = {
            "key": "event",
            "source": "custom",
            "channel_id": 123,
        }
        owner.events = [event]
        owner.state = {"events": [event]}
        channel = MagicMock(spec=discord.VoiceChannel)
        channel.id = 123
        response = MagicMock(status=500, reason="Internal Server Error")
        channel.delete = AsyncMock(
            side_effect=discord.HTTPException(response, "private details"),
        )
        guild = MagicMock()
        guild.get_channel.return_value = channel

        with patch("elbow_helper.features.event_stats.cog.LOGGER.exception"):
            success, message = await owner.delete_custom_event(guild, "event")

        self.assertFalse(success)
        self.assertEqual(
            message,
            "I couldn't delete the event's voice channel right now. Try again in a moment.",
        )
        self.assertNotIn("private details", message)


if __name__ == "__main__":
    unittest.main()
