from __future__ import annotations

import unittest
from unittest.mock import AsyncMock
from unittest.mock import patch

from elbow_helper.discord.message_search import DiscordMessageSearch
from elbow_helper.discord.message_search import DiscordMessageHistoryError
from elbow_helper.discord.message_search import DiscordMessageSearchError


class DiscordMessageSearchTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def history_message(message_id, *, channel_id=34):
        return {
            "id": str(message_id), "channel_id": str(channel_id),
            "content": f"message {message_id}",
            "timestamp": "2026-09-14T00:00:00+00:00",
            "author": {"id": "56", "username": "Member"},
        }

    async def test_author_channel_and_snowflake_filters_are_sent_to_discord(self):
        http = AsyncMock()
        http.request.return_value = {"messages": [], "total_results": 0}
        await DiscordMessageSearch(http).search(
            guild_id=1, content="", limit=5, channel_ids=(12,), author_id=34, min_id=56, max_id=78,
        )
        params = http.request.await_args.kwargs["params"]
        self.assertIn(("channel_id", "12"), params)
        self.assertIn(("author_id", "34"), params)
        self.assertIn(("min_id", "56"), params)
        self.assertIn(("max_id", "78"), params)
        self.assertIn(("offset", 0), params)
        self.assertNotIn("content", dict(params))
    async def test_long_index_delay_is_not_shortened_into_early_retry(self):
        http = AsyncMock()
        http.request.return_value = {"code": 110000, "retry_after": 30}
        with self.assertRaises(DiscordMessageSearchError):
            await DiscordMessageSearch(http).search(guild_id=1, content="test", limit=5)
        self.assertEqual(http.request.await_count, 1)

    async def test_search_normalizes_nested_results(self) -> None:
        http = AsyncMock()
        http.request.return_value = {
            "total_results": 1,
            "messages": [
                [
                    {
                        "id": "12",
                        "channel_id": "34",
                        "content": "war rule decision",
                        "timestamp": "2026-09-14T00:00:00+00:00",
                        "author": {
                            "id": "56",
                            "username": "Ahmad",
                            "global_name": "Ahmad BE",
                        },
                    }
                ]
            ]
        }

        results = await DiscordMessageSearch(http).search(
            guild_id=1,
            content="war rule",
            limit=5,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].message_id, 12)
        self.assertEqual(results[0].channel_id, 34)
        self.assertEqual(results[0].author_name, "Ahmad BE")
        request = http.request.await_args
        self.assertEqual(
            request.kwargs["params"],
            [("content", "war rule"), ("limit", 5), ("offset", 0)],
        )

    async def test_search_retries_while_discord_indexes_history(self) -> None:
        http = AsyncMock()
        http.request.side_effect = [
            {"code": 110000, "retry_after": 0.5},
            {"messages": [], "total_results": 0},
        ]

        with patch(
            "elbow_helper.discord.message_search.asyncio.sleep",
            new=AsyncMock(),
        ) as sleep:
            results = await DiscordMessageSearch(http).search(
                guild_id=1,
                content="policy",
                limit=5,
            )

        self.assertEqual(results, ())
        sleep.assert_awaited_once_with(0.5)

    async def test_search_stops_after_bounded_index_retries(self) -> None:
        http = AsyncMock()
        http.request.return_value = {"code": 110000, "retry_after": 0}

        with (
            patch(
                "elbow_helper.discord.message_search.asyncio.sleep",
                new=AsyncMock(),
            ),
            self.assertRaisesRegex(
                DiscordMessageSearchError,
                "still indexing",
            ),
        ):
            await DiscordMessageSearch(http).search(
                guild_id=1,
                content="policy",
                limit=5,
            )

        self.assertEqual(http.request.await_count, 3)

    async def test_explicit_pages_use_offsets_and_total_not_short_page_length(self):
        http = AsyncMock()
        http.request.return_value = {
            "total_results": 12,
            "doing_deep_historical_index": False,
            "messages": [[{
                "id": "12", "channel_id": "34", "content": "decision",
                "timestamp": "2026-09-14T00:00:00+00:00",
                "author": {"id": "56", "username": "Member"},
            }]],
        }
        page = await DiscordMessageSearch(http).search_page(
            guild_id=1, content="decision", limit=5, offset=5,
            channel_ids=(34,),
        )

        self.assertEqual(len(page.messages), 1)
        self.assertEqual(page.total_results, 12)
        self.assertEqual(page.next_offset, 10)
        self.assertFalse(page.offset_limit_reached)
        self.assertIn(("offset", 5), http.request.await_args.kwargs["params"])

    async def test_offset_cap_and_deep_indexing_are_explicit(self):
        http = AsyncMock(return_value=None)
        http.request.return_value = {
            "total_results": 20_000,
            "doing_deep_historical_index": True,
            "messages": [],
        }
        page = await DiscordMessageSearch(http).search_page(
            guild_id=1, content="decision", limit=25, offset=9_975,
        )
        self.assertIsNone(page.next_offset)
        self.assertTrue(page.offset_limit_reached)
        self.assertTrue(page.deep_historical_indexing)

    async def test_missing_coverage_metadata_and_invalid_offsets_fail_closed(self):
        http = AsyncMock()
        http.request.return_value = {"messages": []}
        with self.assertRaises(DiscordMessageSearchError):
            await DiscordMessageSearch(http).search_page(
                guild_id=1, content="decision", limit=5,
            )
        for offset in (True, -1, 9_976):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                await DiscordMessageSearch(http).search_page(
                    guild_id=1, content="decision", limit=5, offset=offset,
                )

    async def test_malformed_and_duplicate_result_groups_fail_closed(self):
        message = {
            "id": "12", "channel_id": "34", "content": "decision",
            "timestamp": "2026-09-14T00:00:00+00:00",
            "author": {"id": "56", "username": "Member"},
        }
        for groups in (([{}],), ([message], [message])):
            http = AsyncMock()
            http.request.return_value = {
                "messages": list(groups), "total_results": len(groups),
            }
            with self.subTest(groups=groups), self.assertRaises(
                DiscordMessageSearchError
            ):
                await DiscordMessageSearch(http).search_page(
                    guild_id=1, content="decision", limit=5,
                )

    async def test_channel_history_uses_exclusive_before_and_advances_oldest_id(self):
        http = AsyncMock()
        http.request.return_value = [
            self.history_message(90), self.history_message(80),
        ]
        page = await DiscordMessageSearch(http).history_page(
            channel_id=34, before_id=100, after_id=50, limit=2,
        )
        self.assertEqual([message.message_id for message in page.messages], [90, 80])
        self.assertEqual(page.next_before_id, 80)
        self.assertFalse(page.reached_window_start)
        self.assertEqual(
            http.request.await_args.kwargs["params"],
            [("limit", 2), ("before", "100")],
        )

    async def test_channel_history_stops_at_lower_bound_and_short_end(self):
        for payload in (
            [self.history_message(60), self.history_message(50)],
            [self.history_message(60)],
            [],
        ):
            http = AsyncMock()
            http.request.return_value = payload
            with self.subTest(payload=payload):
                page = await DiscordMessageSearch(http).history_page(
                    channel_id=34, before_id=100, after_id=50, limit=2,
                )
                self.assertTrue(page.reached_window_start)
                self.assertIsNone(page.next_before_id)
                self.assertTrue(all(message.message_id > 50 for message in page.messages))

    async def test_channel_history_rejects_malformed_wrong_channel_or_order(self):
        payloads = (
            {},
            [self.history_message(90, channel_id=35)],
            [self.history_message(80), self.history_message(90)],
            [self.history_message(90), self.history_message(90)],
            [
                self.history_message(90), self.history_message(80),
                self.history_message(70),
            ],
            [{"id": "90", "channel_id": "34", "author": {}}],
            [{
                "id": "90", "channel_id": "34", "content": "missing time",
                "author": {"id": "56", "username": "Member"},
            }],
        )
        for payload in payloads:
            http = AsyncMock()
            http.request.return_value = payload
            with self.subTest(payload=payload), self.assertRaises(
                DiscordMessageHistoryError
            ):
                await DiscordMessageSearch(http).history_page(
                    channel_id=34, before_id=100, after_id=50, limit=2,
                )

    async def test_channel_history_rejects_invalid_scope_and_page_size(self):
        reader = DiscordMessageSearch(AsyncMock())
        for values in (
            {"channel_id": 0, "before_id": 100, "after_id": 50, "limit": 2},
            {"channel_id": 34, "before_id": 50, "after_id": 50, "limit": 2},
            {"channel_id": 34, "before_id": 100, "after_id": 50, "limit": 101},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                await reader.history_page(**values)


if __name__ == "__main__":
    unittest.main()
