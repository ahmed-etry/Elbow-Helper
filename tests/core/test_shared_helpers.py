from __future__ import annotations

import asyncio
import logging
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord

from elbow_helper.agent.discord_actions.safety import find_member
from elbow_helper.core.background import start_tracked_task
from elbow_helper.domain.numbers import non_negative_int, positive_int_or_none
from elbow_helper.domain.player_tags import parse_player_tag_list
from elbow_helper.features.recruitment import config as recruitment_config
from elbow_helper.features.recruitment.helpers import new_ticket_name
from elbow_helper.infrastructure.discord.autocomplete import player_choices
from elbow_helper.infrastructure.discord.embeds import build_reopened_ticket_embed
from elbow_helper.infrastructure.discord.message_text import normalize_message
from elbow_helper.infrastructure.discord.temporary_errors import is_temporary_discord_error
from elbow_helper.infrastructure.discord.thread_discovery import fetch_thread


def _http_error(kind: type[discord.HTTPException], status: int) -> discord.HTTPException:
    return kind(SimpleNamespace(status=status, reason="x"), "failed")


class TemporaryErrorTests(unittest.TestCase):
    def test_network_and_timeout_failures_are_temporary(self) -> None:
        for error in (
            asyncio.TimeoutError(), OSError("socket"), aiohttp.ClientConnectionError(),
            aiohttp.ServerDisconnectedError(), ConnectionResetError(),
        ):
            with self.subTest(error=type(error).__name__):
                self.assertTrue(is_temporary_discord_error(error))

    def test_rate_limits_and_server_errors_are_temporary(self) -> None:
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.assertTrue(is_temporary_discord_error(_http_error(discord.HTTPException, status)))

    def test_known_messages_are_temporary_whatever_the_type(self) -> None:
        for text in ("Temporary failure in name resolution", "Cannot connect to host x", "request timed out",
                     "Connection reset by peer", "Server disconnected"):
            with self.subTest(text=text):
                self.assertTrue(is_temporary_discord_error(RuntimeError(text)))

    def test_permanent_failures_are_not_temporary(self) -> None:
        for error in (_http_error(discord.Forbidden, 403), _http_error(discord.NotFound, 404),
                      _http_error(discord.HTTPException, 400), ValueError("bad value")):
            with self.subTest(error=type(error).__name__):
                self.assertFalse(is_temporary_discord_error(error))


class PlayerTagListTests(unittest.TestCase):
    def test_splits_on_commas_semicolons_newlines_and_spaces(self) -> None:
        valid, invalid = parse_player_tag_list("#P0Y, LQG;#GRJ\n#CUV   zzz")

        self.assertEqual(valid, ["#P0Y", "#LQG", "#GRJ", "#CUV"])
        self.assertEqual(invalid, ["zzz"])

    def test_repeats_collapse_and_o_is_read_as_zero(self) -> None:
        valid, invalid = parse_player_tag_list("#POY #P0Y p0y")

        self.assertEqual(valid, ["#P0Y"])
        self.assertEqual(invalid, [])

    def test_empty_input_has_nothing(self) -> None:
        self.assertEqual(parse_player_tag_list(""), ([], []))
        self.assertEqual(parse_player_tag_list(" ,; \n"), ([], []))

    def test_invalid_entries_keep_their_order_and_spelling(self) -> None:
        self.assertEqual(parse_player_tag_list("nope #P0Y bad!"), (["#P0Y"], ["nope", "bad!"]))


class NumberTests(unittest.TestCase):
    def test_positive_int_or_none_accepts_only_real_ids(self) -> None:
        self.assertEqual(positive_int_or_none(5), 5)
        self.assertEqual(positive_int_or_none("12"), 12)
        for value in (0, -3, "0", "-3", "1.5", " 4", True, 2.0, None, b"7"):
            with self.subTest(value=value):
                self.assertIsNone(positive_int_or_none(value))

    def test_non_negative_int_falls_back_to_zero(self) -> None:
        self.assertEqual(non_negative_int("7"), 7)
        self.assertEqual(non_negative_int(3.9), 3)
        for value in (None, "", -4, "x", [], object()):
            with self.subTest(value=value):
                self.assertEqual(non_negative_int(value), 0)


class MessageTextTests(unittest.TestCase):
    def test_removes_mentions_links_and_spoilers_and_collapses_spaces(self) -> None:
        text = "Hi <@1> and <@!2>  <@&3> see <#4>  ||hidden||\nNow   HERE"

        self.assertEqual(normalize_message(text), "hi and see now here")


class TicketStatusTests(unittest.TestCase):
    def test_prefix_pairs_name_the_same_channels_as_before(self) -> None:
        self.assertEqual(recruitment_config.TICKET_STATUS_ORDER, ["🚧", "🔗", "⛔", "⏳", "🤔", "✅"])
        self.assertEqual(recruitment_config.TICKET_RENAME_EMOJIS, ["🚧", "⏳", "🤔", "✅"])
        self.assertEqual(recruitment_config.TRIAL_TICKET_PREFIXES_START, [("🚧ticket-", "⏳ticket-")])
        self.assertEqual(recruitment_config.TRIAL_TICKET_PREFIXES_END, [("⏳ticket-", "🤔ticket-")])
        self.assertEqual(recruitment_config.DECLINED_TICKET_PREFIXES, [
            ("🚧ticket-", "⛔ticket-"), ("⏳ticket-", "⛔ticket-"),
            ("🤔ticket-", "⛔ticket-"), ("ticket-", "⛔ticket-"),
        ])

    def test_new_ticket_name_returns_the_name_without_renaming(self) -> None:
        channel = SimpleNamespace(name="🚧ticket-ana")

        self.assertEqual(new_ticket_name(channel, recruitment_config.DECLINED_TICKET_PREFIXES), "⛔ticket-ana")
        self.assertEqual(channel.name, "🚧ticket-ana")
        self.assertEqual(new_ticket_name(SimpleNamespace(name="🔗ticket-ana"),
                                         recruitment_config.DECLINED_TICKET_PREFIXES), None)
        self.assertEqual(new_ticket_name(SimpleNamespace(name="ticket-ana"),
                                         recruitment_config.DECLINED_TICKET_PREFIXES), "⛔ticket-ana")


class StartTrackedTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_is_logged_with_the_feature_label_and_forgotten(self) -> None:
        tracked: set[asyncio.Task[None]] = set()

        async def fail() -> None:
            raise RuntimeError("boom")

        with self.assertLogs("test.tracked", level="ERROR") as captured:
            task = start_tracked_task(fail(), name="job", tracked=tracked,
                                      logger=logging.getLogger("test.tracked"), label="Clan Reporting")
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)

        self.assertFalse(tracked)
        self.assertIn("Clan Reporting background task failed: job", captured.output[0])

    async def test_running_task_is_tracked_and_cancel_is_not_an_error(self) -> None:
        tracked: set[asyncio.Task[None]] = set()
        started = asyncio.Event()

        async def wait() -> None:
            started.set()
            await asyncio.Event().wait()

        task = start_tracked_task(wait(), name="wait", tracked=tracked,
                                  logger=logging.getLogger("test.tracked"), label="X")
        await started.wait()
        self.assertEqual(tracked, {task})
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

        self.assertFalse(tracked)


class PlayerChoicesTests(unittest.IsolatedAsyncioTestCase):
    async def test_rows_become_choices_cut_to_100_characters(self) -> None:
        rows = [
            {"player_name": "Ana", "clan_code": "EH", "townhall": 16, "player_tag": "#P0Y"},
            {"player_name": "N" * 200, "player_tag": "#LQG"},
            {"player_tag": "#GRJ", "townhall": None},
        ]
        search = MagicMock(return_value=rows)

        choices = await player_choices(search, "an")

        search.assert_called_once_with("an", 25)
        self.assertEqual([choice.value for choice in choices], ["#P0Y", "#LQG", "#GRJ"])
        self.assertEqual(choices[0].name, "Ana - EH - TH16 - #P0Y")
        self.assertEqual(len(choices[1].name), 100)
        self.assertEqual(choices[2].name, "#GRJ - - - TH0 - #GRJ")

    async def test_failures_give_no_choices_and_log_only_when_asked(self) -> None:
        for error in (sqlite3.OperationalError("locked"), KeyError("player_tag"), ValueError("x")):
            with self.subTest(error=type(error).__name__):
                search = MagicMock(side_effect=error)
                self.assertEqual(await player_choices(search, "x"), [])
                with self.assertLogs("test.choices", level="ERROR") as captured:
                    self.assertEqual(await player_choices(
                        search, "x", logger=logging.getLogger("test.choices"), label="Health player autocomplete"), [])
                self.assertIn("Health player autocomplete failed", captured.output[0])

    async def test_a_slow_search_gives_no_choices(self) -> None:
        import elbow_helper.infrastructure.discord.autocomplete as module

        original = module.PLAYER_SEARCH_TIMEOUT_SECONDS
        module.PLAYER_SEARCH_TIMEOUT_SECONDS = 0.01
        try:
            import time
            self.assertEqual(await player_choices(lambda *_: time.sleep(0.2) or [], "x"), [])
            with self.assertLogs("test.choices", level="WARNING") as captured:
                self.assertEqual(await player_choices(
                    lambda *_: time.sleep(0.2) or [], "x", logger=logging.getLogger("test.choices"), label="Slow"), [])
            self.assertIn("Slow timed out", captured.output[0])
        finally:
            module.PLAYER_SEARCH_TIMEOUT_SECONDS = original


class FetchThreadTests(unittest.IsolatedAsyncioTestCase):
    def _bot(self, cached: object = None, fetched: object = None, error: Exception | None = None):
        return SimpleNamespace(
            get_channel=MagicMock(return_value=cached),
            fetch_channel=AsyncMock(return_value=fetched, side_effect=error),
        )

    async def test_cached_thread_is_returned_without_a_fetch(self) -> None:
        thread = MagicMock(spec=discord.Thread)
        bot = self._bot(cached=thread)

        self.assertIs(await fetch_thread(bot, 5), thread)
        bot.fetch_channel.assert_not_awaited()

    async def test_uncached_thread_is_fetched(self) -> None:
        thread = MagicMock(spec=discord.Thread)

        self.assertIs(await fetch_thread(self._bot(fetched=thread), 5), thread)

    async def test_missing_hidden_or_non_thread_channels_give_none(self) -> None:
        for bot in (self._bot(fetched=MagicMock(spec=discord.TextChannel)),
                    self._bot(error=_http_error(discord.NotFound, 404)),
                    self._bot(error=_http_error(discord.Forbidden, 403)),
                    self._bot(error=_http_error(discord.HTTPException, 500))):
            self.assertIsNone(await fetch_thread(bot, 5))


class ReopenedTicketEmbedTests(unittest.TestCase):
    def test_names_the_owner_and_who_reopened_it(self) -> None:
        embed = build_reopened_ticket_embed(
            SimpleNamespace(mention="<@1>"), SimpleNamespace(mention="<@2>"), footer="Support Ticket Controls",
        )

        self.assertEqual(embed.title, "Ticket Reopened")
        self.assertEqual(embed.description, "Restored messaging access for <@1>.")
        self.assertEqual(embed.fields[0].name, "Reopened By")
        self.assertEqual(embed.fields[0].value, "<@2>")
        self.assertEqual(embed.footer.text, "Support Ticket Controls")

    def test_unknown_owner_gets_the_general_line(self) -> None:
        embed = build_reopened_ticket_embed(None, SimpleNamespace(mention="<@2>"), footer="Reactivation Ticket Controls")

        self.assertEqual(embed.description, "Messaging access restored.")
        self.assertEqual(embed.footer.text, "Reactivation Ticket Controls")


class FindMemberTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_member_needs_no_fetch(self) -> None:
        member = object()
        guild = SimpleNamespace(get_member=MagicMock(return_value=member), fetch_member=AsyncMock())

        self.assertIs(await find_member(guild, 1), member)
        guild.fetch_member.assert_not_awaited()

    async def test_uncached_member_is_fetched(self) -> None:
        member = object()
        guild = SimpleNamespace(get_member=MagicMock(return_value=None), fetch_member=AsyncMock(return_value=member))

        self.assertIs(await find_member(guild, 1), member)

    async def test_member_discord_cannot_return_is_none(self) -> None:
        for error in (_http_error(discord.NotFound, 404), _http_error(discord.Forbidden, 403),
                      _http_error(discord.HTTPException, 500)):
            guild = SimpleNamespace(get_member=MagicMock(return_value=None), fetch_member=AsyncMock(side_effect=error))
            self.assertIsNone(await find_member(guild, 1))


if __name__ == "__main__":
    unittest.main()
