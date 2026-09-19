from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.roles import CORE, LEAD
from elbow_helper.features.achievements.queries import (
    AchievementCountRow, AchievementCountSnapshot, AchievementProgressRow,
    CoinTransactionRow, CoinTransactionSnapshot, EconomyRulesSnapshot,
    MemberAchievementSnapshot, MemberInventorySnapshot, RaffleSnapshot,
    RaffleWinnerRow,
)
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.models import AgentRequestContext
from elbow_helper.features.agent.tools.achievements import (
    read_achievement_leaderboard, read_achievement_leaderboard_report,
    read_member_achievement_report, read_member_achievements,
)
from elbow_helper.features.agent.tools.achievement_economy import (
    read_achievement_economy_rules, read_member_coin_history,
    read_member_coin_history_report, read_member_inventory, read_raffle,
    read_raffle_report,
)


class _Queries:
    def economy_rules(self):
        return EconomyRulesSnapshot(
            "2026-09-18T12:00:00+00:00", 5, 10, 1, 4, 10, 100, 1,
            10, 10, 20, (("alpha", 10), ("beta", 15)),
        )

    def member_progress(self, member_id, *, joined_at):
        return MemberAchievementSnapshot(
            "2026-09-18T12:00:00+00:00", member_id, 1, 3,
            (
                AchievementProgressRow(
                    "chatterbox", "Chatterbox", "Send messages", 100,
                    "counter", 75, True, 1000,
                ),
                AchievementProgressRow(
                    "one_of_us", "One of Us", "Stay for a month", 30,
                    "membership_days", 20, False, None,
                ),
                AchievementProgressRow(
                    "storyteller", "Storyteller", "Write a long message", 1,
                    "completion_only", 0, False, None,
                ),
            ),
        )

    def leaderboard_counts(self):
        return AchievementCountSnapshot(
            "2026-09-18T12:00:00+00:00", 25,
            (AchievementCountRow(42, 3), AchievementCountRow(43, 2),
             AchievementCountRow(77, 1)),
        )

    def member_inventory(self, member_id):
        return MemberInventorySnapshot(
            "2026-09-18T12:00:00+00:00", member_id, 125, True, 24321,
        )

    def coin_transactions(self, member_id):
        return CoinTransactionSnapshot(
            "2026-09-18T12:00:00+00:00", member_id, 2,
            (
                CoinTransactionRow(
                    2, -100, "raffle_purchase", "ticket", member_id, 1001,
                ),
                CoinTransactionRow(1, 5, "daily", None, None, 1000),
            ),
            True,
        )

    def raffle(self, month=None):
        if month == "bad":
            raise ValueError("bad month")
        return RaffleSnapshot(
            "2026-09-18T12:00:00+00:00", 24321, "September 2026",
            "Gold Pass", 1, 2, (42, 77), 2,
            (
                RaffleWinnerRow(1, 42, 1000, "draw", None, False),
                RaffleWinnerRow(2, 77, 1001, "reroll", 1, True),
            ),
            True,
        )


class AgentAchievementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requester = SimpleNamespace(
            id=1, display_name="Requester",
            roles=[SimpleNamespace(id=next(iter(CORE)))],
        )
        self.target = SimpleNamespace(
            id=42, display_name="Alpha", joined_at=datetime(
                2026, 8, 1, tzinfo=timezone.utc,
            ), roles=[],
        )
        self.bot_member = SimpleNamespace(id=99, roles=[])
        self.lead = SimpleNamespace(
            id=43, display_name="Lead", roles=[
                SimpleNamespace(id=next(iter(LEAD))),
            ],
        )
        self.channel = SimpleNamespace(
            id=100,
            permissions_for=lambda _: SimpleNamespace(
                view_channel=True, read_message_history=True,
            ),
        )
        members = {
            self.requester.id: self.requester,
            self.target.id: self.target,
            self.lead.id: self.lead,
            self.bot_member.id: self.bot_member,
        }
        self.guild = SimpleNamespace(
            id=10, me=self.bot_member,
            get_member=members.get,
            get_channel_or_thread=lambda value: (
                self.channel if value == self.channel.id else None
            ),
        )
        self.channel.guild = self.guild
        self.context = AgentRequestContext(
            bot=SimpleNamespace(fetch_channel=AsyncMock(return_value=None)),
            guild=self.guild, member=self.requester,
            source_message=SimpleNamespace(channel=self.channel),
            account_links=None, clan_health=None, message_search=None,
            achievement_queries=_Queries(),
        )

    async def test_member_progress_is_retained_filtered_and_paged(self):
        first = await read_member_achievements(self.context, {
            "member_id": 42, "status": "in_progress", "limit": 1,
        })

        self.assertEqual(first["member_name"], "Alpha")
        self.assertEqual(first["completed_count"], 1)
        self.assertEqual(first["matching_rows"], 2)
        self.assertEqual(first["achievements"][0]["achievement_id"], "one_of_us")
        second = await read_member_achievement_report(self.context, {
            "report_id": first["report_id"], "status": "in_progress",
            "offset": first["next_offset"],
        })
        self.assertEqual(
            second["achievements"][0]["progress_kind"], "completion_only",
        )

    async def test_leaderboard_keeps_only_current_server_members(self):
        first = await read_achievement_leaderboard(self.context, {})

        self.assertEqual(first["represented_members"], 1)
        self.assertEqual(first["members"][0]["member_id"], 42)
        self.assertEqual(first["members"][0]["rank"], 1)
        retained = await read_achievement_leaderboard_report(self.context, {
            "report_id": first["report_id"],
        })
        self.assertEqual(retained, first)

    async def test_requester_permission_loss_hides_retained_report(self):
        first = await read_member_achievements(
            self.context, {"member_id": 42},
        )
        self.requester.roles = []

        with self.assertRaises(AgentAccessLost):
            await read_member_achievement_report(self.context, {
                "report_id": first["report_id"],
            })

    async def test_access_loss_during_query_does_not_retain_result(self):
        with patch(
            "elbow_helper.features.agent.tools.achievements.require_evidence_access",
            AsyncMock(side_effect=[None, AgentAccessLost("revoked")]),
        ):
            with self.assertRaises(AgentAccessLost):
                await read_achievement_leaderboard(self.context, {})

        self.assertEqual(self.context.state.reports, {})

    async def test_missing_or_departed_member_is_not_queried(self):
        result = await read_member_achievements(
            self.context, {"member_id": 77},
        )
        self.assertIn("error", result)
        self.assertEqual(self.context.state.reports, {})

    async def test_self_inventory_needs_no_additional_role(self):
        result = await read_member_inventory(
            self.context, {"member_id": self.requester.id},
        )

        self.assertEqual(result["balance"], 125)
        self.assertTrue(result["has_current_ticket"])
        self.assertEqual(self.context.state.required_access, set())

    async def test_economy_rules_are_authoritative_and_permission_rechecked(self):
        result = await read_achievement_economy_rules(self.context, {})

        self.assertEqual(result["daily_activity"]["qualifying_messages"], 5)
        self.assertEqual(result["tickets"]["cost_coins"], 100)
        self.assertEqual(result["manual_monthly_caps"]["combined_coins"], 20)
        self.assertEqual(result["achievement_reward_total_coins"], 25)

        class _RevokingQueries(_Queries):
            def economy_rules(inner):
                self.requester.roles = []
                return super().economy_rules()

        self.context = replace(
            self.context, achievement_queries=_RevokingQueries(),
        )
        with self.assertRaises(AgentAccessLost):
            await read_achievement_economy_rules(self.context, {})

    async def test_other_inventory_requires_and_retains_current_lead_access(self):
        denied = await _capture_access_loss(
            read_member_inventory(self.context, {"member_id": self.target.id}),
        )
        self.assertTrue(denied)

        self.requester.roles.append(SimpleNamespace(id=next(iter(LEAD))))
        result = await read_member_inventory(
            self.context, {"member_id": self.target.id},
        )
        self.assertEqual(result["member_name"], "Alpha")
        self.assertIn("lead", self.context.state.required_access)

        self.requester.roles = [
            role for role in self.requester.roles if role.id not in LEAD
        ]
        with self.assertRaises(AgentAccessLost):
            await read_member_inventory(
                self.context, {"member_id": self.target.id},
            )

    async def test_lead_loss_during_other_inventory_read_blocks_result(self):
        self.requester.roles.append(SimpleNamespace(id=next(iter(LEAD))))

        class _RevokingQueries(_Queries):
            def member_inventory(inner, member_id):
                self.requester.roles = [
                    role for role in self.requester.roles if role.id not in LEAD
                ]
                return super().member_inventory(member_id)

        self.context = replace(
            self.context, achievement_queries=_RevokingQueries(),
        )
        with self.assertRaises(AgentAccessLost):
            await read_member_inventory(
                self.context, {"member_id": self.target.id},
            )

    async def test_coin_history_is_retained_and_paged(self):
        first = await read_member_coin_history(
            self.context, {"member_id": self.target.id, "limit": 1},
        )
        self.assertEqual(first["total_transactions"], 2)
        self.assertTrue(first["complete_snapshot"])
        self.assertEqual(first["transactions"][0]["transaction_id"], 2)

        second = await read_member_coin_history_report(self.context, {
            "report_id": first["report_id"],
            "offset": first["next_offset"],
        })
        self.assertEqual(second["transactions"][0]["transaction_id"], 1)

    async def test_coin_history_permission_loss_hides_retained_report(self):
        first = await read_member_coin_history(
            self.context, {"member_id": self.target.id},
        )
        self.requester.roles = []
        with self.assertRaises(AgentAccessLost):
            await read_member_coin_history_report(self.context, {
                "report_id": first["report_id"],
            })

    async def test_raffle_is_retained_paged_and_preserves_unknown_members(self):
        first = await read_raffle(self.context, {"month": "2026-09", "limit": 1})

        self.assertEqual(first["month_label"], "September 2026")
        self.assertEqual(first["prize"], "Gold Pass")
        self.assertEqual(first["total_tickets"], 2)
        self.assertEqual(first["tickets"][0]["member_name"], "Alpha")
        self.assertEqual(first["unresolved_member_count"], 1)
        second = await read_raffle_report(self.context, {
            "report_id": first["report_id"],
            "ticket_offset": first["next_ticket_offset"],
            "winner_offset": first["next_winner_offset"],
        })
        self.assertIsNone(second["tickets"][0]["member_name"])
        self.assertEqual(second["winners"][0]["member_id"], 77)

    async def test_raffle_rejects_invalid_month_and_permission_loss(self):
        invalid = await read_raffle(self.context, {"month": "bad"})
        self.assertIn("YYYY-MM", invalid["error"])
        first = await read_raffle(self.context, {})
        self.requester.roles = []
        with self.assertRaises(AgentAccessLost):
            await read_raffle_report(self.context, {
                "report_id": first["report_id"],
            })


async def _capture_access_loss(awaitable):
    try:
        await awaitable
    except AgentAccessLost:
        return True
    return False


if __name__ == "__main__":
    unittest.main()
