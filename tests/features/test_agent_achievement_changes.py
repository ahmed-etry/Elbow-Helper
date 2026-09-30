"""Achievement command previews include rewards and recheck the award state."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from elbow_helper.features.achievements.achievements import AchievementServiceMixin
from elbow_helper.features.achievements.economy import AchievementEconomyMixin
from elbow_helper.features.agent.actions.contracts import ActionClass
from elbow_helper.features.agent.commands.adapters.achievements import (
    achievement_adapters, prepare_achievement_award, prepare_achievement_remove,
    prepare_grant_coins, prepare_grant_ticket, run_achievement_award,
    run_achievement_remove, run_grant_coins, run_grant_ticket,
)


class _Store(AchievementServiceMixin):
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript("""
            CREATE TABLE achievements (id TEXT, name TEXT);
            CREATE TABLE user_achievements (
                user_id INTEGER, achievement_id TEXT, completed_date INTEGER
            );
            CREATE TABLE coin_transactions (
                user_id INTEGER, type TEXT, reason TEXT
            );
            INSERT INTO achievements VALUES ('one_of_us', 'One of Us');
            INSERT INTO achievements VALUES ('one_more', 'One More');
        """)

    async def _retry_db_operation(self, operation, *args):
        return await operation(self.connection.cursor(), *args)


class _EconomyStore(AchievementEconomyMixin):
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript("""
            CREATE TABLE user_coins (
                user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0,
                manual_cwl_month INTEGER, manual_cwl_awarded INTEGER DEFAULT 0,
                manual_enc_month INTEGER, manual_enc_awarded INTEGER DEFAULT 0
            );
        """)

    def _month_key(self):
        return 24117

    def _is_leadership_any(self, member):
        return False

    async def _ensure_coin_row(self, cursor, user_id):
        cursor.execute("INSERT OR IGNORE INTO user_coins (user_id) VALUES (?)", (user_id,))

    async def _add_coins(self, cursor, user_id, amount, kind, reason, actor_id):
        cursor.execute("UPDATE user_coins SET balance = balance + ? WHERE user_id = ?",
                       (amount, user_id))

    async def _retry_db_operation(self, operation, *args):
        result = await operation(self.connection.cursor(), *args)
        self.connection.commit()
        return result


class AchievementChangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_coin_grant_keeps_caps_and_amount_limit(self):
        store = _EconomyStore()
        try:
            member = SimpleNamespace(id=7, display_name="Member", roles=[])
            actor = SimpleNamespace(id=8, roles=[])
            ok, message = await store.grant_coins_to_member(
                member, "cwl", 12, "reason", actor,
            )
            self.assertTrue(ok)
            self.assertIn("10 coins", message)
            state = await store.manual_coin_grant_state(member, "cwl", 1, actor)
            self.assertEqual(state["balance"], 10)
            self.assertEqual(state["issue"], "CWL cap reached (10/10).")
            denied, _ = await store.grant_coins_to_member(
                member, "cwl", 1, "reason", actor,
            )
            self.assertFalse(denied)
            self.assertEqual(
                (await store.manual_coin_grant_state(member, "encouragement", 1, actor))["issue"],
                None,
            )
        finally:
            store.connection.close()

    async def test_coin_and_ticket_grants_preview_before_feature_call(self):
        member = SimpleNamespace(id=7, mention="<@7>", display_name="Member")
        coin = {"month_key": 24117, "balance": 4, "cwl_awarded": 0,
                "enc_awarded": 0, "issue": None}
        ticket = {"month_key": 24117, "has_ticket": False, "open": True,
                  "issue": None}

        async def grant_coins(*args):
            coin["balance"] += 3
            return True, "Gave 3 coins to Member (cwl)."

        async def grant_ticket(*args):
            ticket["has_ticket"] = True
            return True, "Ticket granted for this month."

        workflow = SimpleNamespace(
            manual_coin_grant_state=AsyncMock(side_effect=lambda *args: dict(coin)),
            grant_coins_to_member=AsyncMock(side_effect=grant_coins),
            ticket_grant_state=AsyncMock(side_effect=lambda *args: dict(ticket)),
            grant_raffle_ticket=AsyncMock(side_effect=grant_ticket),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=SimpleNamespace(get_member=lambda _: member),
            member=SimpleNamespace(display_name="Lead"),
        )
        coin_values = {"user": 7, "category": "cwl", "amount": 3, "reason": "Help"}
        coin_preview = await prepare_grant_coins(context, coin_values)
        self.assertIn("Balance: 4 to 7", coin_preview.lines)
        self.assertTrue(await coin_preview.recheck())
        workflow.grant_coins_to_member.assert_not_awaited()
        await run_grant_coins(context, coin_values)
        self.assertFalse(await coin_preview.recheck())
        ticket_values = {"user": 7, "reason": "Help"}
        ticket_preview = await prepare_grant_ticket(context, ticket_values)
        self.assertTrue(await ticket_preview.recheck())
        workflow.grant_raffle_ticket.assert_not_awaited()
        await run_grant_ticket(context, ticket_values)
        self.assertFalse(await ticket_preview.recheck())

    async def test_name_resolution_and_prior_coin_state(self):
        store = _Store()
        try:
            state = await store.achievement_change_state(7, "One of Us")
            self.assertEqual(state["id"], "one_of_us")
            self.assertIsNone(state["completed_date"])
            self.assertIsNone(await store.achievement_change_state(7, "missing"))
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                await store.achievement_change_state(7, "one")
            store.connection.execute(
                "INSERT INTO user_achievements VALUES (7, 'one_of_us', 100)"
            )
            store.connection.execute(
                "INSERT INTO coin_transactions VALUES (7, 'achievement', 'one_of_us')"
            )
            state = await store.achievement_change_state(7, "one_of_us")
            self.assertEqual(state["completed_date"], 100)
            self.assertTrue(state["reversal_due"])
        finally:
            store.connection.close()

    async def test_award_and_remove_use_feature_operations_after_preview(self):
        state = {"id": "one_of_us", "name": "One of Us", "completed_date": None,
                 "reward": 2, "reversal_due": False}
        member = SimpleNamespace(id=7, mention="<@7>", display_name="Member")

        async def read_state(member_id, query):
            self.assertEqual(member_id, 7)
            return dict(state)

        async def award(*args):
            state["completed_date"] = 100
            state["reversal_due"] = True
            return True, "Awarded **One of Us** ✅"

        async def remove(*args):
            state["completed_date"] = None
            state["reversal_due"] = False
            return True, "Removed **One of Us** ✅"

        workflow = SimpleNamespace(
            bot=SimpleNamespace(get_channel=lambda _: object()),
            achievement_change_state=AsyncMock(side_effect=read_state),
            manually_award_achievement=AsyncMock(side_effect=award),
            manually_remove_achievement=AsyncMock(side_effect=remove),
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(get_cog=lambda _: workflow),
            guild=SimpleNamespace(get_member=lambda _: member),
            member=SimpleNamespace(display_name="Lead"),
        )
        values = {"user": 7, "achievement": "One of Us"}
        preview = await prepare_achievement_award(context, values)
        self.assertIn("Add 2 coins.", preview.lines)
        self.assertTrue(any("announcement" in line for line in preview.lines))
        self.assertTrue(await preview.recheck())
        workflow.manually_award_achievement.assert_not_awaited()
        outcome = await run_achievement_award(context, values)
        self.assertEqual(outcome.visibility, "private")
        self.assertFalse(await preview.recheck())
        removal = await prepare_achievement_remove(context, values)
        self.assertIn("Remove 2 coins.", removal.lines)
        self.assertTrue(await removal.recheck())
        await run_achievement_remove(context, values)
        self.assertFalse(await removal.recheck())
        self.assertTrue(all(
            adapter.classification is ActionClass.IRREVERSIBLE
            for adapter in achievement_adapters()
            if adapter.path.startswith("/achievement ")
        ))
