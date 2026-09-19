from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote

from elbow_helper.configuration.clans import CLANS
from elbow_helper.configuration.roles import CORE
from elbow_helper.features.account_links.evidence import (
    member_account_evidence, refresh_account_locations,
)
from elbow_helper.features.agent.access import AgentAccessLost
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.agent.reports.roles import RoleAccountReport
from elbow_helper.features.agent.tools.roles import (
    audit_role_accounts, compare_role_account_reports,
    find_discord_roles, read_role_account_report,
    refresh_role_account_report,
)
from elbow_helper.features.agent.reports.role_comparisons import (
    compare_role_account_reports as compare_reports,
)
from elbow_helper.infrastructure.clash.client import ClashResponse


def _account(tag, **changes):
    return dict(player_tag=tag, player_name_last_seen="Player", last_seen_clan_tag=CLANS["BEC"].tag,
                last_seen_clan_code="BEC", last_seen_role="member", **changes)


def _links(rows, response=None):
    return SimpleNamespace(
        get_links_for_members=lambda ids: {value: rows.get(value, []) for value in ids},
        get_player_location=lambda tag: None,
        clash_client=SimpleNamespace(get=AsyncMock(return_value=response)),
    )


def _context(count=12):
    role = SimpleNamespace(id=CLANS["BEC"].member_role_id, name="BEC member", position=3, managed=False, permissions=[])
    members = [SimpleNamespace(id=value, display_name=f"Member {value:02}", roles=[role]) for value in range(1, count + 1)]
    core_role = SimpleNamespace(id=next(iter(CORE)))
    requester = SimpleNamespace(id=999, display_name="Requester", roles=[core_role])
    bot_member = SimpleNamespace(id=998, display_name="Bot", roles=[])
    guild = SimpleNamespace(
        id=1, chunked=True, members=members, roles=[role],
        get_role=lambda value: role if value == role.id else None,
        get_member=lambda value: requester if value == requester.id else None,
        me=bot_member, filesize_limit=10_000_000,
    )
    channel = SimpleNamespace(
        id=100, guild=guild,
        permissions_for=lambda _: SimpleNamespace(
            view_channel=True, read_message_history=True,
        ),
    )
    guild.get_channel_or_thread = lambda value: channel if value == channel.id else None
    return SimpleNamespace(
        guild=guild, member=requester, state=AgentTurnState(),
        source_message=SimpleNamespace(channel=channel),
        bot=SimpleNamespace(fetch_channel=AsyncMock(return_value=None)),
        account_links=_links({1: [_account("#2PP"), _account("#2PQ")]}),
    ), role


class AgentReportTests(unittest.IsolatedAsyncioTestCase):
    async def test_comparison_separates_ownership_location_and_evidence_changes(self):
        context, role = _context(count=2)
        first = await audit_role_accounts(context, {
            "role_ids": [role.id], "refresh_locations": False,
        })
        before = context.state.reports[first["report_id"]]
        members = deepcopy(before.members)
        transferred = members[0]["accounts"].pop(0)
        transferred.update(
            clan_tag=CLANS["BEH"].tag, clan_code="BEH",
            clan_name=CLANS["BEH"].name, location_status="refreshed",
            checked_at="2026-09-18T12:00:00+00:00",
        )
        members[1]["accounts"].append(transferred)
        members[1]["matched_roles"] = [role.name, "Second role"]
        members[0]["accounts"][0]["checked_at"] = "2026-09-18T13:00:00+00:00"
        after = RoleAccountReport(
            "after", "2026-09-18T14:00:00+00:00", before.roles,
            tuple(members),
        )
        context.state.reports[after.report_id] = after

        result = await compare_role_account_reports(context, {
            "before_report_id": before.report_id,
            "after_report_id": after.report_id,
        })

        self.assertEqual(
            result["account_counts"],
            {"added": 0, "removed": 0, "changed": 1, "unchanged": 1},
        )
        self.assertEqual(result["member_counts"]["changed"], 2)
        account = next(
            row for row in result["changes"]
            if row["entity"] == "account"
        )
        self.assertEqual(account["player_tag"], "#2PP")
        self.assertEqual(
            account["classifications"],
            ["ownership_changed", "location_changed", "evidence_status_changed"],
        )
        self.assertIn("member_id", account["changed_fields"])
        self.assertNotIn("checked_at", account["changed_fields"])
        self.assertEqual(result["ignored_account_fields"], ["checked_at"])
        self.assertTrue(result["complete_comparison"])

    async def test_comparison_counts_all_entities_and_pages_changes(self):
        context, role = _context(count=2)
        characters = "0289PYLQGRJCUV"
        tags = [f"#{left}{right}" for left in characters for right in characters][:30]
        context.account_links = _links({1: [_account(tag) for tag in tags]})
        first = await audit_role_accounts(context, {
            "role_ids": [role.id], "refresh_locations": False,
        })
        before = context.state.reports[first["report_id"]]
        members = deepcopy(before.members)
        members[1]["accounts"] = members[0]["accounts"]
        members[0]["accounts"] = []
        after = RoleAccountReport("after", "later", before.roles, tuple(members))
        context.state.reports[after.report_id] = after
        arguments = {
            "before_report_id": before.report_id,
            "after_report_id": after.report_id, "limit": 25,
        }
        rows = []
        while True:
            result = await compare_role_account_reports(context, arguments)
            rows.extend(result["changes"])
            if result["next_offset"] is None:
                break
            arguments["offset"] = result["next_offset"]

        self.assertEqual(result["account_counts"]["changed"], 30)
        self.assertEqual(result["member_counts"]["changed"], 2)
        self.assertEqual(result["role_counts"]["unchanged"], 1)
        self.assertEqual(result["total_changes"], 32)
        self.assertEqual(len(rows), 32)
        self.assertTrue(all(
            row["classifications"] == ["ownership_changed"]
            for row in rows if row["entity"] == "account"
        ))

    async def test_comparison_handles_role_selection_and_identical_reports(self):
        context, role = _context(count=1)
        first = await audit_role_accounts(context, {
            "role_ids": [role.id], "refresh_locations": False,
        })
        report = context.state.reports[first["report_id"]]
        identical = compare_reports(report, report)
        self.assertEqual(identical["total_changes"], 0)
        changed = RoleAccountReport(
            "roles", "later",
            ({"role_id": role.id, "name": "Renamed"},
             {"role_id": 123, "name": "Added"}),
            report.members,
        )
        result = compare_reports(report, changed)
        self.assertEqual(
            result["role_counts"],
            {"added": 1, "removed": 0, "changed": 1, "unchanged": 0},
        )
        self.assertEqual(
            [row["entity"] for row in result["changes"][:2]],
            ["role", "role"],
        )

    async def test_comparison_rejects_missing_wrong_kind_duplicate_and_access_loss(self):
        context, role = _context(count=1)
        first = await audit_role_accounts(context, {
            "role_ids": [role.id], "refresh_locations": False,
        })
        report = context.state.reports[first["report_id"]]
        context.state.reports["wrong"] = SimpleNamespace()
        for report_id in ("missing", "wrong"):
            result = await compare_role_account_reports(context, {
                "before_report_id": report.report_id,
                "after_report_id": report_id,
            })
            self.assertIn("error", result)
        duplicate_members = RoleAccountReport(
            "duplicate", "later", report.roles,
            (report.members[0], report.members[0]),
        )
        with self.assertRaises(ValueError):
            compare_reports(report, duplicate_members)
        context.member.roles = []
        with self.assertRaises(AgentAccessLost):
            await compare_role_account_reports(context, {
                "before_report_id": report.report_id,
                "after_report_id": report.report_id,
            })

    async def test_bounded_refresh_creates_immutable_revision_and_advances_failures(self):
        context, role = _context(count=1)
        characters = "0289PYLQGRJCUV"
        tags = [f"#{left}{right}" for left in characters for right in characters][:30]
        context.account_links = _links({1: [_account(tag) for tag in tags]})
        context.account_links.clash_client.get.return_value = ClashResponse(
            503, None, {}, 1, 1,
        )
        first = await audit_role_accounts(context, {
            "role_ids": [role.id], "refresh_locations": False,
        })
        source = context.state.reports[first["report_id"]]

        second = await refresh_role_account_report(context, {
            "report_id": source.report_id,
        })
        revision = context.state.reports[second["report_id"]]
        third = await refresh_role_account_report(context, {
            "report_id": revision.report_id,
        })

        self.assertEqual(second["parent_report_id"], source.report_id)
        self.assertEqual(second["refresh_batch_count"], 25)
        self.assertEqual(second["remaining_unattempted_accounts"], 5)
        self.assertEqual(third["refresh_batch_count"], 5)
        self.assertEqual(third["remaining_unattempted_accounts"], 0)
        self.assertEqual(len(source.refresh_attempted_player_tags), 0)
        self.assertEqual(len(revision.refresh_attempted_player_tags), 25)
        self.assertEqual(
            len(context.state.reports[third["report_id"]].refresh_attempted_player_tags),
            30,
        )
        self.assertTrue(all(
            account["location_status"] == "last_known"
            for account in source.members[0]["accounts"]
        ))
        self.assertEqual(context.account_links.clash_client.get.await_count, 30)

    async def test_refresh_updates_location_only_and_preserves_source_identity(self):
        context, role = _context(count=1)

        async def profile(path, **_):
            tag = unquote(path.removeprefix("/players/"))
            return ClashResponse(200, {
                "tag": tag, "name": "Observed", "role": "admin",
                "clan": {"tag": CLANS["BEH"].tag, "name": CLANS["BEH"].name},
            }, {}, 1, 1)

        context.account_links.clash_client.get.side_effect = profile
        first = await audit_role_accounts(context, {
            "role_ids": [role.id], "refresh_locations": False,
        })
        source = context.state.reports[first["report_id"]]
        before = dict(source.members[0]["accounts"][0])
        result = await refresh_role_account_report(context, {
            "report_id": source.report_id, "player_tags": ["2pp"],
        })
        refreshed = context.state.reports[result["report_id"]]
        after = refreshed.members[0]["accounts"][0]

        self.assertEqual(source.members[0]["accounts"][0], before)
        self.assertEqual(refreshed.parent_report_id, source.report_id)
        self.assertEqual(refreshed.refresh_attempted_player_tags, ("#2PP",))
        self.assertEqual(after["location_status"], "refreshed")
        self.assertEqual(after["clan_code"], "BEH")
        self.assertEqual(after["player_name"], "Observed")
        self.assertEqual(refreshed.members[0]["member_id"], source.members[0]["member_id"])
        self.assertEqual(after["player_tag"], before["player_tag"])
        self.assertEqual(after["primary"], before["primary"])

    async def test_refresh_rejects_invalid_foreign_duplicate_and_resolved_tags(self):
        context, role = _context(count=1)
        first = await audit_role_accounts(context, {
            "role_ids": [role.id], "refresh_locations": False,
        })
        report_id = first["report_id"]
        cases = (
            (["not-a-tag"], "valid"),
            (["#2PP", "2pp"], "once"),
            (["#PPP"], "belong"),
        )
        for tags, error in cases:
            with self.subTest(tags=tags):
                result = await refresh_role_account_report(context, {
                    "report_id": report_id, "player_tags": tags,
                })
                self.assertIn(error, result["error"])
        members = deepcopy(context.state.reports[report_id].members)
        members[0]["accounts"][0]["location_status"] = "refreshed"
        resolved = RoleAccountReport(
            "resolved", "2026-09-18", (), tuple(members),
        )
        context.state.reports[resolved.report_id] = resolved
        result = await refresh_role_account_report(context, {
            "report_id": resolved.report_id, "player_tags": ["#2PP"],
        })
        self.assertIn("unresolved", result["error"])
        context.account_links.clash_client.get.assert_not_awaited()

    async def test_access_loss_during_refresh_retains_no_revision(self):
        context, role = _context(count=1)
        first = await audit_role_accounts(context, {
            "role_ids": [role.id], "refresh_locations": False,
        })
        existing = tuple(context.state.reports)

        async def revoke_access(*_, **__):
            context.member.roles = []
            return ClashResponse(503, None, {}, 1, 1)

        context.account_links.clash_client.get.side_effect = revoke_access
        with self.assertRaises(AgentAccessLost):
            await refresh_role_account_report(context, {
                "report_id": first["report_id"], "player_tags": ["#2PP"],
            })
        self.assertEqual(tuple(context.state.reports), existing)

    async def test_feature_refresh_is_bounded_and_does_not_mutate_input(self):
        context, _ = _context(count=1)
        account = {
            "player_tag": "#2PP", "player_name": "Before", "primary": True,
            "clan_tag": None, "clan_code": None, "clan_name": None,
            "clan_role": None, "in_clan": None, "location_status": "unknown",
            "checked_at": None,
        }
        context.account_links.clash_client.get.return_value = ClashResponse(
            503, None, {}, 1, 1,
        )
        refreshed = await refresh_account_locations(context.account_links, [account])
        self.assertEqual(account["location_status"], "unknown")
        self.assertEqual(refreshed[0]["location_status"], "refresh_failed")
        with self.assertRaisesRegex(ValueError, "exceeds 25"):
            await refresh_account_locations(context.account_links, [account] * 26)

    async def test_all_pages_account_for_every_member_in_a_large_role(self):
        context, role = _context(count=67)
        first = await audit_role_accounts(context, {
            "role_ids": [role.id], "refresh_locations": False,
        })
        member_ids = [member["member_id"] for member in first["members"]]
        page = first
        while page["next_offset"] is not None:
            page = await read_role_account_report(context, {
                "report_id": first["report_id"],
                "offset": page["next_offset"],
                "limit": 25,
            })
            member_ids.extend(member["member_id"] for member in page["members"])

        self.assertEqual(first["total_members"], 67)
        self.assertEqual(first["members_without_links"], 66)
        self.assertEqual(len(member_ids), 67)
        self.assertEqual(set(member_ids), set(range(1, 68)))
        context.account_links.clash_client.get.assert_not_awaited()

    async def test_role_union_includes_each_member_once_even_with_duplicate_names(self):
        context, first_role = _context(count=3)
        second_role = SimpleNamespace(id=1234, name="Second role")
        roles = {first_role.id: first_role, second_role.id: second_role}
        context.guild.get_role = roles.get
        context.guild.members[0].roles = [first_role, second_role]
        context.guild.members[1].roles = [second_role]
        context.guild.members[1].display_name = context.guild.members[0].display_name
        context.guild.members[2].roles = []

        result = await audit_role_accounts(context, {
            "role_ids": [first_role.id, second_role.id], "refresh_locations": False,
        })

        self.assertEqual(result["total_members"], 2)
        self.assertEqual([member["member_id"] for member in result["members"]], [1, 2])
        self.assertEqual(result["members"][0]["matched_roles"], [first_role.name, second_role.name])

    async def test_empty_role_returns_a_complete_empty_report(self):
        context, role = _context(count=0)

        result = await audit_role_accounts(context, {"role_ids": [role.id]})

        self.assertEqual(result["total_members"], 0)
        self.assertEqual(result["total_accounts"], 0)
        self.assertEqual(result["members"], [])
        self.assertIsNone(result["next_offset"])
        self.assertIn(result["report_id"], context.state.reports)
        context.account_links.clash_client.get.assert_not_awaited()

    async def test_unknown_role_does_not_create_a_report_or_refresh_accounts(self):
        context, _ = _context()

        result = await audit_role_accounts(context, {"role_ids": [1]})

        self.assertIn("error", result)
        self.assertEqual(context.state.reports, {})
        context.account_links.clash_client.get.assert_not_awaited()

    async def test_full_report_retains_unlinked_members_beyond_first_page(self):
        context, role = _context()
        first = await audit_role_accounts(context, {"role_ids": [role.id], "refresh_locations": False})
        self.assertEqual(first["total_members"], 12)
        self.assertEqual(first["total_accounts"], 2)
        self.assertEqual(first["members_without_links"], 11)
        self.assertEqual(len(first["members"]), 10)
        second = await read_role_account_report(context, {"report_id": first["report_id"], "offset": first["next_offset"]})
        self.assertEqual(len(second["members"]), 2)
        unresolved = await read_role_account_report(context, {"report_id": first["report_id"], "selection": "no_links", "limit": 25})
        self.assertEqual(len(unresolved["members"]), 11)
        outside = await read_role_account_report(context, {"report_id": first["report_id"], "selection": "outside_clan", "clan_code": "BEH"})
        self.assertEqual(outside["matching_members"], 0)

    async def test_refresh_distinguishes_no_clan_from_failed_lookup(self):
        links = _links({1: [_account("#2PP"), _account("#2PQ")]})
        links.clash_client.get.side_effect = [ClashResponse(200, {"tag": "#2PP", "name": "Unclanned"}, {}, 1, 1),
                                             ClashResponse(503, None, {}, 1, 1)]
        result = await member_account_evidence(links, [1, 2], refresh=True)
        self.assertFalse(result[1][0]["in_clan"])
        self.assertIsNone(result[1][0]["clan_tag"])
        self.assertEqual(result[1][0]["location_status"], "refreshed")
        self.assertTrue(result[1][0]["checked_at"])
        self.assertEqual(result[1][1]["clan_code"], "BEC")
        self.assertEqual(result[1][1]["location_status"], "refresh_failed")
        self.assertIsNone(result[1][1]["checked_at"])
        self.assertEqual(result[2], [])

    async def test_refresh_timeout_retains_every_account_and_cancels_requests(self):
        links = _links({1: [_account("#2PP"), _account("#2PQ")]})
        cancelled = []
        async def delayed(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        links.clash_client.get.side_effect = delayed
        with patch("elbow_helper.features.account_links.evidence.LOCATION_REFRESH_SECONDS", 0.01):
            result = await member_account_evidence(links, [1], refresh=True)
        self.assertEqual(len(result[1]), 2)
        self.assertTrue(all(row["location_status"] == "refresh_failed" for row in result[1]))
        self.assertEqual(len(cancelled), 2)

    async def test_incomplete_member_cache_does_not_produce_complete_report(self):
        context, role = _context()
        context.guild.chunked = False
        context.guild.chunk = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, "Complete guild membership"):
            await audit_role_accounts(context, {"role_ids": [role.id], "refresh_locations": False})
        self.assertEqual(context.state.reports, {})

    async def test_role_discovery_uses_configured_purpose_and_full_membership(self):
        context, role = _context()
        role.name = "Brown Elbow Cat"
        result = await find_discord_roles(context, {"query": "BEC"})
        self.assertEqual(result["roles"][0]["member_count"], 12)
        self.assertIn({"clan_code": "BEC", "purpose": "member"}, result["roles"][0]["clan_purposes"])
