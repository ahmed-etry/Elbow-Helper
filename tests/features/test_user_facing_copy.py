from __future__ import annotations

import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import discord

from elbow_helper.features.cwl.bonus.config import BonusConfigRepository
from elbow_helper.features.event_stats.cog import EventStatsCog


class ApprovedCopyTests(unittest.IsolatedAsyncioTestCase):


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
