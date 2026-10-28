from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from elbow_helper.infrastructure.discord.publishing import PublishingQueue
from elbow_helper.features.clan_reporting.cog import ClanReporting
from elbow_helper.features.clan_reporting.config import MISSING_ELDER_STICKY_BURIAL_MESSAGES


class MissingElderBoardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rows = [{"discord_user_id": 42, "player_name": "One"}]
        self.messages = {}
        self.next_message_id = 100
        self.channel = SimpleNamespace(
            send=AsyncMock(side_effect=self.send),
            fetch_message=AsyncMock(side_effect=self.fetch_message),
            history=MagicMock(side_effect=self.history),
        )
        self.manager = self.new_manager({"missing_elder_messages": {}})
        self.save_state = self.enterContext(patch(
            "elbow_helper.features.clan_reporting.elders.save_state",
        ))

    def new_manager(self, state):
        manager = object.__new__(ClanReporting)
        manager.bot = SimpleNamespace(publishing=MagicMock(wraps=PublishingQueue()))
        manager.state = state
        manager._board_last_repost_at = {}
        manager._board_repost_locks = {}
        manager.account_links = SimpleNamespace(
            get_missing_elder_rows=lambda _: self.rows,
        )
        manager._get_leadership_channel = AsyncMock(return_value=self.channel)
        return manager

    def message(self, *, board=True):
        self.next_message_id += 1
        message = SimpleNamespace(
            id=self.next_message_id,
            embeds=[discord.Embed(title="Missing Elder Rank — BEH (1)")] if board else [],
        )

        async def delete():
            self.messages.pop(message.id, None)

        message.delete = AsyncMock(side_effect=delete)
        message.edit = AsyncMock(return_value=message)
        self.messages[message.id] = message
        return message

    async def send(self, *, embed, view):
        message = self.message()
        message.embeds = [embed]
        return message

    async def fetch_message(self, message_id):
        if message_id not in self.messages:
            raise self.http_error(discord.NotFound, 404)
        return self.messages[message_id]

    async def history(self, *, limit, oldest_first=False, after=None):
        messages = sorted(self.messages.values(), key=lambda message: message.id, reverse=True)
        if after is not None:
            messages = [message for message in messages if message.id > after.id]
        for message in messages[:limit]:
            yield message

    @staticmethod
    def http_error(error_type, status):
        return error_type(SimpleNamespace(status=status, reason="synthetic"), "synthetic")

    def bury(self):
        for _ in range(MISSING_ELDER_STICKY_BURIAL_MESSAGES):
            trigger = self.message(board=False)
        return trigger

    async def assert_quiet_activity(self):
        self.channel.history.reset_mock()
        for _ in range(3):
            self.assertFalse(await self.manager._repost_missing_elder_message_from_activity(
                "BEH", self.message(board=False),
            ))
        self.channel.history.assert_not_called()
        self.channel.send.assert_not_awaited()

    async def test_empty_refresh_deletes_tracked_board_and_activity_stays_quiet(self):
        board = self.message()
        self.manager.state["missing_elder_messages"]["BEH"] = board.id
        self.manager._board_last_repost_at["BEH"] = 123.0
        self.rows = []

        self.assertTrue(await self.manager._update_missing_elder_message("BEH"))

        board.delete.assert_awaited_once()
        board.edit.assert_not_awaited()
        self.assertIsNone(self.manager.state["missing_elder_messages"]["BEH"])
        self.assertNotIn("BEH", self.manager._board_last_repost_at)
        self.save_state.assert_called()
        await self.assert_quiet_activity()
        self.assertTrue(await self.manager._update_missing_elder_message("BEH"))
        self.channel.history.assert_not_called()
        self.channel.send.assert_not_awaited()

    async def test_empty_refresh_recovers_and_deletes_buried_board_without_reposting(self):
        board = self.message()
        self.bury()
        self.rows = []

        self.assertTrue(await self.manager._update_missing_elder_message(
            "BEH", reposition_if_buried=True,
        ))

        board.delete.assert_awaited_once()
        self.channel.history.assert_called_once()
        await self.assert_quiet_activity()

    async def test_empty_channel_is_remembered_without_further_history_scans(self):
        self.rows = []

        self.assertTrue(await self.manager._update_missing_elder_message("BEH"))

        self.channel.history.assert_called_once()
        self.assertIsNone(self.manager.state["missing_elder_messages"]["BEH"])
        await self.assert_quiet_activity()

    async def test_rows_return_posts_fresh_board_and_sticky_behavior_resumes(self):
        old_board = self.message()
        self.manager.state["missing_elder_messages"]["BEH"] = old_board.id
        self.rows = []
        await self.manager._update_missing_elder_message("BEH")
        self.rows = [{"discord_user_id": 42, "player_name": "One"}]
        self.channel.history.reset_mock()

        self.assertTrue(await self.manager._update_missing_elder_message("BEH"))

        self.channel.history.assert_not_called()
        new_id = self.manager.state["missing_elder_messages"]["BEH"]
        self.assertNotEqual(new_id, old_board.id)
        board = self.messages[new_id]
        self.assertEqual(board.embeds[0].title, "Missing Elder Rank — BEH (1)")
        self.assertEqual(board.embeds[0].description, "- <@42> - One")
        self.assertTrue(await self.manager._repost_missing_elder_message_from_activity(
            "BEH", self.bury(),
        ))
        board.delete.assert_awaited_once()
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_activity_deletes_tracked_board_when_rows_became_empty(self):
        board = self.message()
        self.manager.state["missing_elder_messages"]["BEH"] = board.id
        trigger = self.bury()
        self.rows = []

        self.assertTrue(await self.manager._repost_missing_elder_message_from_activity("BEH", trigger))

        board.delete.assert_awaited_once()
        await self.assert_quiet_activity()

    async def test_restart_refresh_recovers_untracked_board_instead_of_sending(self):
        board = self.message()
        self.manager = self.new_manager({"missing_elder_messages": {}})

        self.assertTrue(await self.manager._update_missing_elder_message("BEH"))

        self.assertEqual(self.manager.state["missing_elder_messages"]["BEH"], board.id)
        self.manager.bot.publishing.edit_message.assert_called_once()
        board.edit.assert_awaited_once()
        self.channel.send.assert_not_awaited()

    async def test_restart_activity_recovers_and_reposts_buried_board(self):
        board = self.message()
        trigger = self.bury()
        self.manager = self.new_manager({"missing_elder_messages": {}})

        self.assertTrue(await self.manager._repost_missing_elder_message_from_activity("BEH", trigger))

        board.delete.assert_awaited_once()
        self.channel.send.assert_awaited_once()
        self.assertNotEqual(self.manager.state["missing_elder_messages"]["BEH"], board.id)

    async def test_restart_activity_recovers_empty_board_and_deletes_it(self):
        board = self.message()
        trigger = self.bury()
        self.rows = []
        self.manager = self.new_manager({"missing_elder_messages": {}})

        self.assertTrue(await self.manager._repost_missing_elder_message_from_activity("BEH", trigger))

        board.delete.assert_awaited_once()
        await self.assert_quiet_activity()

    async def test_restart_remembers_intentional_absence_and_rows_can_return(self):
        self.rows = []
        await self.manager._update_missing_elder_message("BEH")
        state = {"missing_elder_messages": dict(self.manager.state["missing_elder_messages"])}
        self.manager = self.new_manager(state)

        await self.assert_quiet_activity()
        self.rows = [{"discord_user_id": 42, "player_name": "One"}]
        self.assertTrue(await self.manager._update_missing_elder_message("BEH"))
        self.channel.send.assert_awaited_once()

    async def test_delete_failure_preserves_tracking_and_allows_retry(self):
        board = self.message()
        self.manager.state["missing_elder_messages"]["BEH"] = board.id
        self.rows = []
        board.delete.side_effect = self.http_error(discord.Forbidden, 403)

        with self.assertLogs("elbow_helper.features.clan_reporting.elders", level="WARNING"):
            self.assertFalse(await self.manager._update_missing_elder_message("BEH"))

        self.assertEqual(self.manager.state["missing_elder_messages"]["BEH"], board.id)
        self.channel.send.assert_not_awaited()
        board.delete.side_effect = None
        self.assertTrue(await self.manager._update_missing_elder_message("BEH"))
        self.assertIsNone(self.manager.state["missing_elder_messages"]["BEH"])

    async def test_deleted_board_during_edit_posts_replacement(self):
        board = self.message()
        self.manager.state["missing_elder_messages"]["BEH"] = board.id

        async def deleted(**fields):
            self.messages.pop(board.id, None)
            raise self.http_error(discord.NotFound, 404)

        board.edit.side_effect = deleted
        with self.assertLogs("elbow_helper.discord.publishing", "WARNING"):
            self.assertTrue(await self.manager._update_missing_elder_message("BEH"))
        self.channel.send.assert_awaited_once()
        self.assertNotEqual(self.manager.state["missing_elder_messages"]["BEH"], board.id)

    async def test_edit_failure_preserves_board_and_does_not_raise_into_refresh(self):
        board = self.message()
        self.manager.state["missing_elder_messages"]["BEH"] = board.id
        board.edit.side_effect = self.http_error(discord.HTTPException, 503)
        with patch("elbow_helper.infrastructure.discord.publishing.HTTP_RETRY_DELAYS", (0, 0)):
            with self.assertLogs("elbow_helper.discord.publishing", "WARNING") as logs:
                self.assertFalse(await self.manager._update_missing_elder_message("BEH"))
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(board.edit.await_count, 3)
        self.assertEqual(self.manager.state["missing_elder_messages"]["BEH"], board.id)
        self.channel.send.assert_not_awaited()

    async def test_delete_not_found_records_intentional_absence(self):
        board = self.message()
        self.manager.state["missing_elder_messages"]["BEH"] = board.id
        self.rows = []
        board.delete.side_effect = self.http_error(discord.NotFound, 404)

        self.assertTrue(await self.manager._update_missing_elder_message("BEH"))

        await self.assert_quiet_activity()

    async def test_rate_limited_delete_preserves_tracking(self):
        board = self.message()
        self.manager.state["missing_elder_messages"]["BEH"] = board.id
        self.rows = []
        board.delete.side_effect = self.http_error(discord.HTTPException, 429)

        with (
            patch("elbow_helper.features.clan_reporting.helpers.asyncio.sleep", new_callable=AsyncMock),
            self.assertLogs("elbow_helper.features.clan_reporting.helpers", level="WARNING"),
        ):
            self.assertFalse(await self.manager._update_missing_elder_message("BEH"))

        self.assertEqual(self.manager.state["missing_elder_messages"]["BEH"], board.id)
        self.channel.send.assert_not_awaited()

    async def test_failed_recovery_does_not_record_intentional_absence(self):
        self.rows = []
        self.channel.history.side_effect = self.http_error(discord.Forbidden, 403)

        self.assertFalse(await self.manager._update_missing_elder_message("BEH"))

        self.assertNotIn("BEH", self.manager.state["missing_elder_messages"])
        self.channel.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
