"""Synthetic hibernation cohorts keep workflow membership through join-date ranking."""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.channels import HIBERNATION_LOG
from elbow_helper.agent.capabilities.hibernation.reads import read_active_hibernation
from elbow_helper.agent.member_names import member_label
from elbow_helper.agent.models import AgentTurnState
from elbow_helper.agent.research.members import find_discord_members, read_discord_members
from elbow_helper.features.hibernation.queries import HibernationQueries


class IntentCohortTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.members = [SimpleNamespace(
            id=i, display_name=f"Member {i}", name=f"user{i}", nick=f"Member {i}",
            bot=False, mention=f"<@{i}>", roles=[SimpleNamespace(id=50)],
            joined_at=datetime(2024, 1, i, tzinfo=timezone.utc),
        ) for i in range(1, 14)]
        self.members[0].display_name = "Chungus (The Explorer)"
        self.members[0].joined_at = datetime(2023, 1, 12, tzinfo=timezone.utc)
        self.members[0].roles = []
        outsider = replace_member(self.members[1], id=90,
                                  joined_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
        unknown = replace_member(self.members[2], id=98, joined_at=None)
        second_outsider = replace_member(outsider, id=91)
        self.members.extend([outsider, second_outsider, unknown])
        # Same role count as current workflow members, but one member swapped out.
        state = {str(i): {"hibernation_date": f"<t:{1750000000 + i}:F>"}
                 for i in [*range(1, 14), 98, 99]}
        self.guild = SimpleNamespace(
            id=3, chunked=True, members=self.members,
            get_member=lambda i: next((m for m in self.members if m.id == i), None),
        )
        self.context = SimpleNamespace(
            guild=self.guild, state=AgentTurnState(), account_links=None,
            hibernation_queries=HibernationQueries(lambda: state),
        )
        self.guards = [
            patch("elbow_helper.agent.research.members.require_evidence_access", AsyncMock()),
            patch("elbow_helper.agent.research.members.accessible_message_channel", AsyncMock(return_value=object())),
            patch("elbow_helper.agent.capabilities.hibernation.reads.require_evidence_access", AsyncMock()),
            patch("elbow_helper.agent.capabilities.hibernation.reads.accessible_message_channel", AsyncMock(return_value=object())),
        ]
        for guard in self.guards:
            guard.start()
            self.addCleanup(guard.stop)

    async def test_full_retained_cohort_is_sorted_before_paging_and_ignores_role_swap(self):
        report = await read_active_hibernation(self.context, {"limit": 1})
        self.assertEqual(len(report["records"]), 1)
        args = {"hibernation_report_id": report["report_id"], "sort": "oldest_first", "limit": 10}
        first = await read_discord_members(self.context, args)
        self.assertEqual([m["member_id"] for m in first["members"]], list(range(1, 11)))
        self.assertEqual(first["missing_member_ids"], [99])
        self.assertEqual(first["cohort"]["unknown_join_member_ids"], [98])
        self.assertEqual(first["cohort"]["record_count"], 15)
        self.assertEqual(first["total_members"], 14)
        self.assertIn(HIBERNATION_LOG, self.context.state.source_channels)
        self.members.reverse()
        second = await read_discord_members(self.context, args)
        self.assertEqual(first, second)
        tail = await read_discord_members(self.context, {**args, "offset": first["next_offset"]})
        self.assertEqual([m["member_id"] for m in tail["members"]], [11, 12, 13, 98])
        role = await read_discord_members(self.context, {"role_ids": [50], "sort": "oldest_first", "limit": 10})
        self.assertEqual(role["total_members"], first["cohort"]["record_count"])
        self.assertEqual(role["members"][0]["member_id"], 90)

    async def test_ties_and_unknown_joins_have_stable_order_in_both_directions(self):
        self.members[2].joined_at = self.members[1].joined_at
        for sort, expected in (("oldest_first", [2, 3, 98]), ("newest_first", [2, 3, 98])):
            result = await read_discord_members(self.context, {"member_ids": [98, 3, 2], "sort": sort})
            self.assertEqual([m["member_id"] for m in result["members"]], expected)

    async def test_join_window_applies_within_retained_cohort(self):
        report = await read_active_hibernation(self.context, {"limit": 1})
        result = await read_discord_members(self.context, {
            "hibernation_report_id": report["report_id"], "sort": "oldest_first",
            "joined_before": "2024-01-01T00:00:00Z",
        })
        self.assertEqual([m["member_id"] for m in result["members"]], [1])
        self.assertEqual(result["missing_member_ids"], [99])

    async def test_report_selection_checks_identity_access_and_complete_guild_cache(self):
        page = await read_active_hibernation(self.context, {})
        args = {"hibernation_report_id": page["report_id"], "sort": "oldest_first"}
        for extra in ({"member_ids": [1]}, {"role_ids": [50]}, {"hibernation_report_id": "missing"}):
            self.assertIn("error", await read_discord_members(self.context, {**args, **extra}))
        report = self.context.state.reports[page["report_id"]]
        self.context.state.reports[page["report_id"]] = replace(report, guild_id=4)
        self.assertIn("error", await read_discord_members(self.context, args))
        self.context.state.reports[page["report_id"]] = report
        with patch("elbow_helper.agent.research.members.accessible_message_channel", AsyncMock(return_value=None)):
            self.assertIn("error", await read_discord_members(self.context, args))
        self.guild.chunked = False
        async def chunk(**kwargs):
            self.guild.chunked = True
        self.guild.chunk = AsyncMock(side_effect=chunk)
        await read_discord_members(self.context, args)
        self.guild.chunk.assert_awaited_once_with(cache=True)

    async def test_unique_nicknames_hide_usernames_in_discovery_details_and_previews(self):
        self.members[0].display_name = "Rockstar"
        self.members[0].name = "rockstar061464"
        self.members[0].nick = "Rockstar"
        found = await find_discord_members(self.context, {"query": "rockstar061464"})
        self.assertEqual(found["members"][0]["display_name"], "Rockstar")
        self.assertNotIn("username", found["members"][0])
        result = await read_discord_members(self.context, {"member_ids": [1]})
        self.assertNotIn("username", result["members"][0])
        self.assertEqual(member_label(self.members[0], guild=self.guild), "Rockstar")
        # A collision outside the selected page still needs a disambiguator.
        self.members[1].display_name = "rockstar"
        result = await read_discord_members(self.context, {"member_ids": [1], "limit": 1})
        self.assertEqual(result["members"][0]["username"], "rockstar061464")
        self.assertEqual(member_label(self.members[0], guild=self.guild), "Rockstar (@rockstar061464)")


def replace_member(member, **changes):
    return SimpleNamespace(**{**vars(member), **changes})
