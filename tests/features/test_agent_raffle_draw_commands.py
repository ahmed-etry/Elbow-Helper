"""Raffle previews and feature runs share the eligible ticket state."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.achievements.raffle import AchievementRaffleMixin
from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.capabilities.achievements.commands import (
    achievement_adapters, prepare_raffle_draw, prepare_raffle_reroll,
    run_raffle_draw, run_raffle_reroll,
)


class _Raffle(AchievementRaffleMixin):
    GUILD_ID = 1

    def __init__(self, guild):
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript("""
            CREATE TABLE raffle_tickets (month_key INTEGER, user_id INTEGER);
            CREATE TABLE raffle_winners (
                id INTEGER PRIMARY KEY AUTOINCREMENT, month_key INTEGER,
                user_id INTEGER, drawn_at INTEGER, draw_type TEXT,
                reroll_of INTEGER, is_active INTEGER
            );
            CREATE TABLE coin_transactions (
                user_id INTEGER, amount INTEGER, type TEXT, reason TEXT,
                actor_id INTEGER, created_at INTEGER
            );
            CREATE TABLE economy_meta (key TEXT, value TEXT);
            INSERT INTO raffle_tickets VALUES (24117, 7), (24117, 8);
            INSERT INTO economy_meta VALUES ('reward_24117', 'Prize'),
                                            ('winners_24117', '1');
        """)
        self.bot = SimpleNamespace(get_guild=lambda _: guild)
        self.update_raffle_hub_message = AsyncMock()

    def _month_key(self):
        return 24117

    def _is_leadership_any(self, member):
        return False

    async def _get_meta(self, cursor, key):
        cursor.execute("SELECT value FROM economy_meta WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row[0] if row else None

    async def _set_meta(self, cursor, key, value):
        cursor.execute("INSERT OR REPLACE INTO economy_meta VALUES (?, ?)", (key, value))

    async def _set_raffle_hub_state_internal(self, cursor, state, month_key):
        return None

    async def _retry_db_operation(self, operation, *args):
        result = await operation(self.connection.cursor(), *args)
        self.connection.commit()
        return result


class RaffleDrawCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_draw_and_reroll_preview_and_post_through_feature(self):
        actor = SimpleNamespace(id=4)
        bot_member = SimpleNamespace(id=5)
        members = {member_id: SimpleNamespace(id=member_id)
                   for member_id in (7, 8)}
        channel = SimpleNamespace(
            id=9, mention="<#9>",
            permissions_for=lambda _: SimpleNamespace(
                view_channel=True, send_messages=True,
            ),
            send=AsyncMock(),
        )
        guild = SimpleNamespace(
            id=1, me=bot_member, get_member=lambda member_id: members.get(member_id),
            get_channel_or_thread=lambda _: channel,
        )
        channel.guild = guild
        workflow = _Raffle(guild)
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow), guild=guild,
            member=actor, source_message=SimpleNamespace(channel=channel),
        )
        try:
            draw = await prepare_raffle_draw(context, {})
            self.assertEqual(draw.count, 1)
            self.assertIn("Eligible ticket: <@7>", draw.lines)
            self.assertTrue(await draw.recheck())
            channel.send.assert_not_awaited()
            result = await run_raffle_draw(context, {})
            self.assertEqual(len(result.result["winners"]), 1)
            self.assertFalse(await draw.recheck())
            self.assertIn("Congratulations", channel.send.await_args.args[0])
            reroll = await prepare_raffle_reroll(context, {})
            self.assertTrue(any("Current winner:" in line for line in reroll.lines))
            self.assertTrue(await reroll.recheck())
            await run_raffle_reroll(context, {})
            self.assertEqual(channel.send.await_count, 2)
            self.assertEqual(workflow.update_raffle_hub_message.await_count, 2)
            self.assertTrue(all(
                adapter.classification is ActionClass.IRREVERSIBLE
                for adapter in achievement_adapters()
                if adapter.path in {"/raffle draw", "/raffle reroll"}
            ))
        finally:
            workflow.connection.close()
