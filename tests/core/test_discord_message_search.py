from __future__ import annotations

import unittest
from unittest.mock import AsyncMock
from unittest.mock import patch

from elbow_helper.discord.message_search import DiscordMessageSearch
from elbow_helper.discord.message_search import DiscordMessageSearchError


class DiscordMessageSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_index_delay_is_not_shortened_into_early_retry(self):
        http = AsyncMock()
        http.request.return_value = {"code": 110000, "retry_after": 30}
        with self.assertRaises(DiscordMessageSearchError):
            await DiscordMessageSearch(http).search(guild_id=1, content="test", limit=5)
        self.assertEqual(http.request.await_count, 1)

    async def test_search_normalizes_nested_results(self) -> None:
        http = AsyncMock()
        http.request.return_value = {
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
            [("content", "war rule"), ("limit", 5)],
        )

    async def test_search_retries_while_discord_indexes_history(self) -> None:
        http = AsyncMock()
        http.request.side_effect = [
            {"code": 110000, "retry_after": 0.5},
            {"messages": []},
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


if __name__ == "__main__":
    unittest.main()
