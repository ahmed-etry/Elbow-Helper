from __future__ import annotations

import logging
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from elbow_helper.infrastructure.discord.channel_ordering import reorder_ticket_channels


def _error(kind: type[discord.HTTPException], status: int = 403) -> discord.HTTPException:
    return kind(SimpleNamespace(status=status, reason="x"), "denied")


class _Channel:
    def __init__(self, channel_id: int, name: str, position: int, created: int, *, can_manage: bool = True):
        self.id = channel_id
        self.name = name
        self.position = position
        self.created_at = datetime.fromtimestamp(created, timezone.utc)
        self._can_manage = can_manage

    def permissions_for(self, member: object) -> SimpleNamespace:
        return SimpleNamespace(manage_channels=self._can_manage, view_channel=True)


class _Category:
    def __init__(self, channels: list[_Channel], me: object | None = object()):
        self.text_channels = channels
        self.guild = SimpleNamespace(
            me=me, get_member=MagicMock(return_value=None), fetch_member=AsyncMock(),
        )

    def names(self) -> list[str]:
        return [channel.name for channel in sorted(self.text_channels, key=lambda ch: ch.position)]


def _fake_move(category: _Category, *, fail: dict[int, Exception] | None = None, calls: list[int] | None = None):
    async def move(channel, _category, ordered, target_index, *, reason):
        if calls is not None:
            calls.append(channel.id)
        if fail and channel.id in fail:
            raise fail[channel.id]
        lines = [item for item in sorted(ordered, key=lambda ch: ch.position) if item is not channel]
        lines.insert(target_index, channel)
        for position, item in enumerate(lines):
            item.position = position
    return move


def _run_args(category: _Category, **overrides):
    warn, clear = MagicMock(), MagicMock()
    args = dict(
        is_ticket=lambda channel: True,
        sort_key=lambda channel: channel.created_at.timestamp(),
        reason="Organize tests",
        logger=logging.getLogger("test.reorder"),
        warn=warn,
        clear=clear,
    )
    args.update(overrides)
    return args, warn, clear


class ReorderTicketChannelsTests(unittest.IsolatedAsyncioTestCase):
    def _category(self, **kwargs) -> _Category:
        return _Category([
            _Channel(1, "c", 0, 300), _Channel(2, "a", 1, 100), _Channel(3, "b", 2, 200),
        ], **kwargs)

    async def test_moves_channels_into_sort_order_and_reports_success(self) -> None:
        category = self._category()
        args, warn, clear = _run_args(category)
        with patch("elbow_helper.infrastructure.discord.channel_ordering.move_text_channel_within_category",
                   _fake_move(category)):
            result = await reorder_ticket_channels(category, SimpleNamespace(id=9), **args)

        self.assertTrue(result)
        self.assertEqual(category.names(), ["a", "b", "c"])
        warn.assert_not_called()
        clear.assert_called_once_with("member_resolve", "member_missing")

    async def test_only_channels_the_feature_calls_tickets_are_moved(self) -> None:
        category = self._category()
        category.text_channels.append(_Channel(4, "notice", 3, 1))
        args, _, _ = _run_args(category, is_ticket=lambda channel: channel.name != "notice")
        with patch("elbow_helper.infrastructure.discord.channel_ordering.move_text_channel_within_category",
                   _fake_move(category)):
            await reorder_ticket_channels(category, SimpleNamespace(id=9), **args)

        self.assertEqual(category.names(), ["a", "b", "c", "notice"])

    async def test_no_tickets_is_a_success_with_no_moves(self) -> None:
        category = self._category()
        args, _, _ = _run_args(category, is_ticket=lambda channel: False)
        move = AsyncMock()
        with patch("elbow_helper.infrastructure.discord.channel_ordering.move_text_channel_within_category", move):
            result = await reorder_ticket_channels(category, SimpleNamespace(id=9), **args)

        self.assertTrue(result)
        move.assert_not_called()

    async def test_already_ordered_channels_are_left_alone(self) -> None:
        category = _Category([_Channel(1, "a", 0, 100), _Channel(2, "b", 1, 200)])
        args, _, _ = _run_args(category)
        move = AsyncMock()
        with patch("elbow_helper.infrastructure.discord.channel_ordering.move_text_channel_within_category", move):
            self.assertTrue(await reorder_ticket_channels(category, SimpleNamespace(id=9), **args))

        move.assert_not_called()

    async def test_bot_member_is_fetched_when_not_cached(self) -> None:
        category = self._category(me=None)
        category.guild.fetch_member.return_value = object()
        args, warn, _ = _run_args(category)
        with patch("elbow_helper.infrastructure.discord.channel_ordering.move_text_channel_within_category",
                   _fake_move(category)):
            self.assertTrue(await reorder_ticket_channels(category, SimpleNamespace(id=9), **args))

        category.guild.fetch_member.assert_awaited_once_with(9)
        warn.assert_not_called()

    async def test_unresolvable_bot_member_skips_the_reorder_and_warns(self) -> None:
        category = self._category(me=None)
        category.guild.fetch_member.side_effect = _error(discord.NotFound, 404)
        args, warn, clear = _run_args(category)
        move = AsyncMock()
        with patch("elbow_helper.infrastructure.discord.channel_ordering.move_text_channel_within_category", move):
            result = await reorder_ticket_channels(category, SimpleNamespace(id=9), **args)

        self.assertFalse(result)
        move.assert_not_called()
        self.assertEqual(warn.call_args.args[0], "member_resolve")
        clear.assert_not_called()

    async def test_missing_bot_user_skips_the_reorder_and_warns(self) -> None:
        category = self._category(me=None)
        args, warn, _ = _run_args(category)
        result = await reorder_ticket_channels(category, None, **args)

        self.assertFalse(result)
        self.assertEqual(warn.call_args.args[0], "member_missing")

    async def test_channel_without_manage_permission_is_skipped_and_reported(self) -> None:
        category = _Category([
            _Channel(1, "c", 0, 300), _Channel(2, "a", 1, 100, can_manage=False), _Channel(3, "b", 2, 200),
        ])
        args, _, _ = _run_args(category)
        calls: list[int] = []
        with patch("elbow_helper.infrastructure.discord.channel_ordering.move_text_channel_within_category",
                   _fake_move(category, calls=calls)):
            result = await reorder_ticket_channels(category, SimpleNamespace(id=9), **args)

        self.assertFalse(result)
        self.assertNotIn(2, calls)

    async def test_first_forbidden_move_stops_the_pass_and_is_raised(self) -> None:
        category = self._category()
        args, _, _ = _run_args(category)
        calls: list[int] = []
        forbidden = _error(discord.Forbidden)
        with patch("elbow_helper.infrastructure.discord.channel_ordering.move_text_channel_within_category",
                   _fake_move(category, fail={2: forbidden}, calls=calls)):
            with self.assertRaises(discord.Forbidden) as caught:
                await reorder_ticket_channels(category, SimpleNamespace(id=9), **args)

        self.assertIs(caught.exception, forbidden)
        self.assertEqual(calls, [2])

    async def test_skip_forbidden_moves_the_rest_then_raises(self) -> None:
        category = _Category([
            _Channel(1, "d", 0, 400), _Channel(2, "c", 1, 300), _Channel(3, "a", 2, 100), _Channel(4, "b", 3, 200),
        ])
        args, _, _ = _run_args(category, skip_forbidden=True)
        calls: list[int] = []
        with patch("elbow_helper.infrastructure.discord.channel_ordering.move_text_channel_within_category",
                   _fake_move(category, fail={3: _error(discord.Forbidden)}, calls=calls)):
            with self.assertRaises(discord.Forbidden):
                await reorder_ticket_channels(category, SimpleNamespace(id=9), **args)

        self.assertEqual(calls.count(3), 1)
        self.assertGreater(len(calls), 1)

    async def test_other_discord_failure_stops_and_is_raised(self) -> None:
        category = self._category()
        args, _, _ = _run_args(category, skip_forbidden=True)
        calls: list[int] = []
        failure = _error(discord.HTTPException, 500)
        with patch("elbow_helper.infrastructure.discord.channel_ordering.move_text_channel_within_category",
                   _fake_move(category, fail={2: failure}, calls=calls)):
            with self.assertRaises(discord.HTTPException) as caught:
                await reorder_ticket_channels(category, SimpleNamespace(id=9), **args)

        self.assertIs(caught.exception, failure)
        self.assertEqual(calls, [2])

    async def test_pause_follows_each_attempt_only_when_asked(self) -> None:
        for pause, expected in ((0.0, 0), (1.0, 2)):
            category = self._category()
            args, _, _ = _run_args(category, pause_seconds=pause)
            sleep = AsyncMock()
            with (
                patch("elbow_helper.infrastructure.discord.channel_ordering.move_text_channel_within_category",
                      _fake_move(category)),
                patch("elbow_helper.infrastructure.discord.channel_ordering.asyncio.sleep", sleep),
            ):
                await reorder_ticket_channels(category, SimpleNamespace(id=9), **args)

            self.assertEqual(sleep.await_count, expected)
            if expected:
                sleep.assert_awaited_with(1.0)

    async def test_ticket_set_changing_mid_reorder_stops_quietly(self) -> None:
        category = self._category()
        args, _, _ = _run_args(category)

        async def move_and_drop(channel, _category, ordered, target_index, *, reason):
            category.text_channels.remove(next(ch for ch in category.text_channels if ch.id == 1))

        with patch("elbow_helper.infrastructure.discord.channel_ordering.move_text_channel_within_category",
                   move_and_drop):
            result = await reorder_ticket_channels(category, SimpleNamespace(id=9), **args)

        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
