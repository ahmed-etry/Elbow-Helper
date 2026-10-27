from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from elbow_helper.features.examination.followups import ExaminationFollowupMixin


def _discord_error(status: int) -> discord.HTTPException:
    error_type = {
        403: discord.Forbidden,
        404: discord.NotFound,
    }.get(status, discord.HTTPException)
    return error_type(SimpleNamespace(status=status, reason="synthetic failure"), "failure")


class ExaminationFollowupTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _owner(*, age_hours: int = 20, stage: str = "initial") -> ExaminationFollowupMixin:
        waiting_since = datetime.now(timezone.utc) - timedelta(hours=age_hours)
        case = {
            "ticket_channel_id": 123,
            "routing_message_id": 456,
            "type": "elder_promo",
            "stage": stage,
            "wait_started_at": waiting_since.isoformat(),
            "last_ping_at": waiting_since.isoformat(),
            "pinged_ids": [11],
            "pinged_role_ids": [33],
        }
        ticket_channel = MagicMock(spec=discord.TextChannel)
        ticket_channel.id = 123
        routing_message = MagicMock(spec=discord.Message)
        routing_message.delete = AsyncMock()
        routing_message.reply = AsyncMock()
        routing_channel = MagicMock(spec=discord.TextChannel)
        routing_channel.fetch_message = AsyncMock(return_value=routing_message)
        routing_channel.send = AsyncMock()

        owner = object.__new__(ExaminationFollowupMixin)
        owner.bot = MagicMock()
        owner.bot.wait_until_ready = AsyncMock()
        owner.bot.get_channel.return_value = ticket_channel
        owner.bot.fetch_channel = AsyncMock(return_value=ticket_channel)
        owner._get_cases = MagicMock(return_value={"123": case})
        owner._get_routing_channel = AsyncMock(return_value=routing_channel)
        owner._waiting_since = AsyncMock(return_value=waiting_since)
        owner._save = MagicMock()
        owner._collect_examiners = MagicMock(return_value=[])
        owner._get_examiner_roster = MagicMock(return_value={})
        owner._match_examiners = MagicMock(return_value=[SimpleNamespace(id=22)])
        owner._resolve_notification_targets = MagicMock(return_value=([44], None))
        owner._build_notification_mentions = MagicMock(return_value="<@22>")
        return owner

    async def _run_pass(self, owner: ExaminationFollowupMixin) -> None:
        with (
            patch(
                "elbow_helper.features.examination.followups.asyncio.sleep",
                new=AsyncMock(side_effect=[None, asyncio.CancelledError]),
            ),
            self.assertRaises(asyncio.CancelledError),
        ):
            await owner._followup_loop()

    async def test_ticket_fetch_errors_preserve_case_and_retry_next_pass(self) -> None:
        for status in (403, 429, 500):
            with self.subTest(status=status):
                owner = self._owner()
                cases = owner._get_cases.return_value
                original = deepcopy(cases)
                routing_channel = owner._get_routing_channel.return_value
                ticket_channel = owner.bot.get_channel.return_value
                owner.bot.get_channel.return_value = None
                owner.bot.fetch_channel.side_effect = [_discord_error(status), ticket_channel]

                await self._run_pass(owner)

                self.assertEqual(cases, original)
                owner._save.assert_not_called()
                routing_channel.fetch_message.assert_not_awaited()
                routing_channel.send.assert_not_awaited()

                await self._run_pass(owner)

                self.assertEqual(owner.bot.fetch_channel.await_count, 2)
                self.assertEqual(cases["123"]["stage"], "reminder")
                routing_channel.fetch_message.return_value.reply.assert_awaited_once()
                routing_channel.fetch_message.return_value.delete.assert_not_awaited()

    async def test_only_ticket_not_found_removes_case_and_routing_message(self) -> None:
        owner = self._owner()
        owner.bot.get_channel.return_value = None
        owner.bot.fetch_channel.side_effect = _discord_error(404)
        routing_message = owner._get_routing_channel.return_value.fetch_message.return_value

        await self._run_pass(owner)

        self.assertEqual(owner._get_cases.return_value, {})
        routing_message.delete.assert_awaited_once_with()
        routing_message.reply.assert_not_awaited()
        owner._save.assert_called_once_with()

    async def test_unresolved_ticket_is_preserved_without_confirmed_deletion(self) -> None:
        for missing_id in (False, True):
            with self.subTest(missing_id=missing_id):
                owner = self._owner()
                cases = owner._get_cases.return_value
                if missing_id:
                    cases["123"].pop("ticket_channel_id")
                original = deepcopy(cases)
                owner.bot.get_channel.return_value = None
                owner.bot.fetch_channel.return_value = MagicMock(spec=discord.CategoryChannel)
                routing_channel = owner._get_routing_channel.return_value

                await self._run_pass(owner)

                self.assertEqual(cases, original)
                routing_channel.fetch_message.assert_not_awaited()
                owner._save.assert_not_called()

    async def test_send_failure_preserves_stage_and_retries_reminder_or_fallback(self) -> None:
        for age_hours, stage, expected_stage in (
            (20, "initial", "reminder"),
            (26, "initial", "fallback"),
            (26, "reminder", "fallback"),
        ):
            for reply in (True, False):
                for status in (403, 404, 500):
                    with self.subTest(stage=stage, expected=expected_stage, reply=reply, status=status):
                        owner = self._owner(age_hours=age_hours, stage=stage)
                        case = owner._get_cases.return_value["123"]
                        original = deepcopy(case)
                        routing_channel = owner._get_routing_channel.return_value
                        routing_message = routing_channel.fetch_message.return_value
                        if reply:
                            send = routing_message.reply
                        else:
                            routing_channel.fetch_message.side_effect = _discord_error(404)
                            send = routing_channel.send
                        send.side_effect = [_discord_error(status), None]

                        await self._run_pass(owner)

                        self.assertEqual(case, original)
                        owner._save.assert_not_called()
                        routing_message.delete.assert_not_awaited()

                        await self._run_pass(owner)

                        self.assertEqual(send.await_count, 2)
                        self.assertEqual(case["stage"], expected_stage)
                        self.assertNotEqual(case["last_ping_at"], original["last_ping_at"])
                        expected_ids = [11, 22] if expected_stage == "fallback" else [22]
                        self.assertCountEqual(case["pinged_ids"], expected_ids)
                        self.assertEqual(case["pinged_role_ids"], [44])
                        owner._save.assert_called_once_with()

    async def test_one_failed_send_does_not_block_other_cases(self) -> None:
        owner = self._owner()
        cases = owner._get_cases.return_value
        cases["124"] = dict(cases["123"], ticket_channel_id=124)
        owner._send_followup = AsyncMock(side_effect=[_discord_error(500), None])

        await self._run_pass(owner)

        self.assertEqual(cases["123"]["stage"], "initial")
        self.assertEqual(cases["124"]["stage"], "reminder")
        self.assertEqual(owner._send_followup.await_count, 2)
        owner._save.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
