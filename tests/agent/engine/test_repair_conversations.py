"""Anonymous conversations exercise real planning, tools, model views and delivery."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from tests.temp_directory import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock


from elbow_helper.configuration.roles import LEAD
from elbow_helper.agent.features.recruitment.report import RecruitmentTrialReport
from elbow_helper.agent.reads.sql.query import run_query
from elbow_helper.agent.reads.sql.sources import DataSource
from elbow_helper.agent.engine.registry import build_agent_tools
from elbow_helper.features.recruitment.queries import RecruitmentQueries
from elbow_helper.features.member_lifecycle.queries import MemberLifecycleQueries
from elbow_helper.agent.features.member_lifecycle.report import MemberLifecycleReport
from tests.agent.request_fixtures import flow_context as _context


def stored_leagues(tier):
    """The record's people reads, holding each account's stored ranked tier."""
    async def summary(tags, as_of=None):
        return {"rows": [{"player_tag": tag, "ranked_league": {"id": 105000034, "name": tier(tag)},
                          "legacy_league": {"name": "Silver III"}, "fields": {"trophies": 0, "bestTrophies": 6000},
                          "field_observed_at": {"leagueTier": 1_790_000_000_000_000},
                          "coverage": {"status": "observed"}} for tag in tags]}
    return SimpleNamespace(account_summary=AsyncMock(side_effect=summary))


class RepairConversationTests(unittest.IsolatedAsyncioTestCase):
    def context(self, fetch):
        context = _context()
        context.member.roles.append(SimpleNamespace(id=next(iter(LEAD))))
        context.guild.members = [context.member]
        context.guild.chunked = True
        context.source_message.archive_reply = False
        context.source_message.mentions = ()
        context.source_message.guild = context.guild
        context.source_message.author = context.member
        context.source_message.reply = AsyncMock(return_value=SimpleNamespace(id=501))
        return replace(context, bot=SimpleNamespace(
            user=SimpleNamespace(id=999), clash_client=SimpleNamespace(configured=True, get=AsyncMock(side_effect=fetch)),
        ))


    async def test_cohort_leagues_count_members_across_accounts_and_page_without_extra_candidates(self):
        context = self.context(lambda *_a, **_kw: None)
        context.bot.people_reads = stored_leagues(lambda tag: "Legend III" if tag in ("#P0", "#Q0") else "Dragon League 30")
        links = {101: [{"player_tag": "#P0"}, {"player_tag": "#P2"}], 102: [{"player_tag": "#Q0"}],
                 103: [{"player_tag": "#Q2"}]}  # 103 is an established member, outside the trial cohort.
        context = replace(context, account_links=SimpleNamespace(get_links_for_members=Mock(
            side_effect=lambda ids: {value: links[value] for value in ids})))
        queries = RecruitmentQueries(lambda: {
            "201": {"applicant_id": 101, "start": "2026-10-01T00:00:00+00:00", "days": 7},
            "202": {"applicant_id": 102, "start": "2026-10-02T00:00:00+00:00", "days": 7},
        }, clock=lambda: datetime(2026, 10, 4, tzinfo=timezone.utc))
        report = RecruitmentTrialReport("cohort", context.guild.id, queries.active_trial_snapshot(ticket_channel_ids=(201, 202)))
        context.state.reports[report.report_id] = report
        context.state.report_sources[report.report_id] = frozenset({91})
        context.state.report_access_requirements[report.report_id] = frozenset({"lead"})
        tool = build_agent_tools()["read_cohort_leagues"]
        first = await tool.handler(context, {"cohort_report_id": "cohort", "league": "Legend", "limit": 2})
        second = await tool.handler(context, first["next_page_arguments"])
        self.assertEqual(first["member_ids"], [101, 102])
        self.assertEqual(first["account_count"], 3)
        rows = first["accounts"] + second["accounts"]
        self.assertEqual({row["player_tag"] for row in rows if row["matches"]}, {"#P0", "#Q0"})
        self.assertEqual({row["member_id"] for row in rows if row["matches"]}, {101, 102})
        self.assertFalse(first["complete"])
        self.assertEqual(first["returned_account_count"], 2)
        self.assertEqual(first["remaining_account_count"], 1)
        self.assertEqual(first["next_page_arguments"], {
            "cohort_report_id": "cohort", "league": "Legend", "limit": 2, "offset": 2})
        self.assertIn("not a complete cohort count", first["instruction"])
        self.assertIsNone(second["next_offset"])
        self.assertIsNone(second["next_page_arguments"])
        self.assertEqual(second["remaining_account_count"], 0)
        self.assertEqual(first["accounts"][0]["best_trophies"], 6000)
        self.assertEqual(first["accounts"][0]["legacy_league"], "Silver III")
        self.assertNotIn(103, context.account_links.get_links_for_members.call_args.args[0])
        complete = await tool.handler(context, {"cohort_report_id": "cohort", "league": "Legend League"})
        self.assertTrue(complete["complete"])
        self.assertEqual(complete["matching_members_in_page"], 2)
        self.assertEqual(complete["matching_accounts_in_page"], 2)
        self.assertEqual(context.bot.people_reads.account_summary.await_count, 3)
        context.bot.clash_client.get.assert_not_awaited()
        context.bot.people_reads = None
        missing = await tool.handler(context, {"cohort_report_id": "cohort", "league": "Legend League"})
        self.assertEqual(missing["cause"], "source_unavailable")


    async def test_join_cohort_filters_discord_join_dates_before_resolving_accounts(self):
        context = self.context(lambda *_a, **_kw: None)
        context.bot.people_reads = stored_leagues(lambda tag: "Legend III")
        links = SimpleNamespace(get_links_for_members=Mock(side_effect=lambda ids: {
            value: [{"player_tag": "#P0" if value == 101 else "#Q0"}] for value in ids}))
        context = replace(context, account_links=links)
        lifecycle = MemberLifecycleQueries(lambda: {"members": {
            "101": {"joined_at_iso": "2026-10-01T00:00:00+00:00", "platform": "Example"},
            "102": {"joined_at_iso": "2026-10-02T00:00:00+00:00", "platform": "Example"},
            "103": {"joined_at_iso": "2025-05-01T00:00:00+00:00", "platform": "Example"},
        }}, clock=lambda: datetime(2026, 10, 4, tzinfo=timezone.utc))
        report = MemberLifecycleReport("joins", context.guild.id, 91, lifecycle.snapshot(
            current_members={101: "Example A", 102: "Example B", 103: "Established member"}, activity_channels={}))
        context.state.reports[report.report_id] = report
        context.state.report_sources[report.report_id] = frozenset({91})
        context.state.report_access_requirements[report.report_id] = frozenset()
        result = await build_agent_tools()["read_cohort_leagues"].handler(context, {
            "cohort_report_id": "joins", "after": "2026-10-01", "before": "2026-11-01", "league": "Legend"})
        self.assertEqual(result["member_ids"], [101, 102])
        self.assertEqual(result["cohort"], "current_stay_joins")
        self.assertEqual(result["matching_members_in_page"], 2)
        links.get_links_for_members.assert_called_once_with([101, 102])


    def test_schema_failure_lists_real_accessible_columns_and_denied_schema_stays_hidden(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sample.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE attacks (player_tag TEXT, defender_townhall INTEGER)")
                connection.commit()
            source = DataSource("sample", path, tables=(("attacks", "lead", "Synthetic attacks"),))
            sql = "SELECT defender_th FROM sample.attacks"
            result, touched = run_query((source,), None, {"lead"}, sql)
            self.assertEqual(result["cause"], "sql_schema_error")
            self.assertEqual(set(result["available_schema"]["sample.attacks"]["columns"]), {"player_tag", "defender_townhall"})
            self.assertEqual(touched, {"lead"})
            denied, _ = run_query((source,), None, set(), sql)
            self.assertNotIn("available_schema", denied)
            self.assertEqual(denied["required_access"], ["lead"])
