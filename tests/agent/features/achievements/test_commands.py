from __future__ import annotations
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from elbow_helper.features.achievements.raffle import AchievementRaffleMixin
from elbow_helper.agent.actions.contracts import ActionClass
from elbow_helper.agent.features.achievements.commands import achievement_adapters
from elbow_helper.agent.features.achievements.raffle_commands import prepare_raffle_draw
from elbow_helper.agent.features.achievements.raffle_commands import prepare_raffle_reroll
from elbow_helper.agent.features.achievements.raffle_commands import run_raffle_draw
from elbow_helper.agent.features.achievements.raffle_commands import run_raffle_reroll
from elbow_helper.agent.features.achievements.raffle_commands import prepare_raffle_prize
from elbow_helper.agent.features.achievements.raffle_commands import prepare_raffle_prize_undo
from elbow_helper.agent.features.achievements.raffle_commands import run_raffle_prize


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
            CREATE TABLE economy_meta (key TEXT PRIMARY KEY, value TEXT);
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

    async def _delete_meta(self, cursor, key):
        cursor.execute("DELETE FROM economy_meta WHERE key = ?", (key,))

    async def _set_raffle_hub_state_internal(self, cursor, state, month_key):
        return None

    async def _retry_db_operation(self, operation, *args):
        result = await operation(self.connection.cursor(), *args)
        self.connection.commit()
        return result


class RaffleDrawCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_draw_whose_announcement_fails_is_not_left_drawn(self):
        members = {member_id: SimpleNamespace(id=member_id) for member_id in (7, 8)}
        channel = SimpleNamespace(id=9, mention="<#9>", send=AsyncMock(
            side_effect=discord.HTTPException(SimpleNamespace(status=500, reason="down"), "down")))
        guild = SimpleNamespace(id=1, me=SimpleNamespace(id=5), get_member=members.get,
                                get_channel_or_thread=lambda _: channel)
        channel.guild = guild
        channel.permissions_for = lambda _: SimpleNamespace(view_channel=True, send_messages=True)
        workflow = _Raffle(guild)
        context = SimpleNamespace(bot=SimpleNamespace(get_cog=lambda _: workflow), guild=guild,
                                  member=SimpleNamespace(id=4),
                                  source_message=SimpleNamespace(channel=channel))
        count = "SELECT COUNT(*) FROM raffle_winners"
        try:
            with self.assertRaises(discord.HTTPException):
                await run_raffle_draw(context, {})
            self.assertEqual(workflow.connection.execute(count).fetchone()[0], 0)
            self.assertEqual(workflow.connection.execute(
                "SELECT COUNT(*) FROM coin_transactions WHERE type = 'raffle_win'").fetchone()[0], 0)
            self.assertIsNone(workflow.connection.execute(
                "SELECT value FROM economy_meta WHERE key = 'last_winner_month'").fetchone())
            workflow.update_raffle_hub_message.assert_not_awaited()
            channel.send.side_effect = None
            await run_raffle_draw(context, {})
            self.assertEqual(workflow.connection.execute(count).fetchone()[0], 1)
            self.assertIn("Congratulations", channel.send.await_args.args[0])
        finally:
            workflow.connection.close()

    async def test_a_reroll_whose_announcement_fails_keeps_the_announced_winners(self):
        members = {member_id: SimpleNamespace(id=member_id) for member_id in (7, 8)}
        channel = SimpleNamespace(id=9, mention="<#9>", send=AsyncMock())
        guild = SimpleNamespace(id=1, me=SimpleNamespace(id=5), get_member=members.get,
                                get_channel_or_thread=lambda _: channel)
        channel.guild = guild
        channel.permissions_for = lambda _: SimpleNamespace(view_channel=True, send_messages=True)
        workflow = _Raffle(guild)
        context = SimpleNamespace(bot=SimpleNamespace(get_cog=lambda _: workflow), guild=guild,
                                  member=SimpleNamespace(id=4),
                                  source_message=SimpleNamespace(channel=channel))

        def saved():
            return [workflow.connection.execute(query).fetchall() for query in (
                "SELECT * FROM raffle_winners ORDER BY id",
                "SELECT rowid, * FROM coin_transactions ORDER BY rowid",
                "SELECT * FROM economy_meta ORDER BY key",
            )]

        try:
            await run_raffle_draw(context, {})
            announced = saved()
            self.assertEqual(len(announced[0]), 1)
            reroll_posts = channel.send.await_count
            hub_updates = workflow.update_raffle_hub_message.await_count
            channel.send.side_effect = discord.HTTPException(
                SimpleNamespace(status=500, reason="down"), "down")
            with self.assertRaises(discord.HTTPException):
                await run_raffle_reroll(context, {})
            self.assertEqual(saved(), announced)
            self.assertEqual(workflow.update_raffle_hub_message.await_count, hub_updates)
            channel.send.side_effect = None
            await run_raffle_reroll(context, {})
            self.assertEqual(channel.send.await_count, reroll_posts + 2)
        finally:
            workflow.connection.close()

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


class RafflePrizeCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_apply_and_undo_use_public_feature_operations(self):
        state = [24117, "Old prize", "2"]

        async def apply(prize, winners):
            state[1:] = [prize, str(winners)]
            return f"Saved this month's raffle prize for {winners} winners: {prize}"

        async def restore(month, prize, winners):
            self.assertEqual(month, state[0])
            state[1:] = [prize, winners]

        workflow = SimpleNamespace(
            raffle_prize_state=AsyncMock(side_effect=lambda: tuple(state)),
            apply_raffle_prize=AsyncMock(side_effect=apply),
            restore_raffle_prize=AsyncMock(side_effect=restore),
        )
        context = SimpleNamespace(bot=SimpleNamespace(get_cog=lambda _: workflow))
        values = {"prize": "New prize", "winners": 3}
        preview = await prepare_raffle_prize(context, values)
        self.assertIn("Prize: Old prize to New prize", preview.lines)
        self.assertTrue(await preview.recheck())
        workflow.apply_raffle_prize.assert_not_awaited()
        outcome = await run_raffle_prize(context, values)
        self.assertEqual(outcome.visibility, "private")
        undo = await prepare_raffle_prize_undo(context, {
            "before": preview.before, "after": outcome.after,
        })
        self.assertTrue(await undo.preview.recheck())
        await undo.run()
        self.assertEqual(state[1:], ["Old prize", "2"])
        workflow.restore_raffle_prize.assert_awaited_once()
