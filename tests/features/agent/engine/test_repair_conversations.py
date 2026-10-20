"""Anonymous conversations exercise real planning, tools, model views and delivery."""

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import discord

from elbow_helper.configuration.roles import LEAD
from elbow_helper.configuration.channels import RECRUITMENT_TICKET_CATEGORY
from elbow_helper.features.agent.capabilities.recruitment.report import RecruitmentTrialReport
from elbow_helper.features.agent.conversation.context import compile_context
from elbow_helper.features.agent.conversation.state import ConversationRecord, ConversationTurn
from elbow_helper.features.agent.datasets.query import run_query
from elbow_helper.features.agent.datasets.sources import DataSource
from elbow_helper.features.agent.delivery import AgentDeliveryMixin
from elbow_helper.features.agent.engine.factual_answers import narrow_answer, clan_count_tag
from elbow_helper.features.agent.engine.grounding import GroundingError
from elbow_helper.features.agent.engine.registry import build_agent_tools
from elbow_helper.features.agent.engine.service import AgentService
from elbow_helper.features.agent.models import AgentTurnState
from elbow_helper.features.recruitment.queries import RecruitmentQueries
from elbow_helper.features.member_lifecycle.queries import MemberLifecycleQueries
from elbow_helper.features.agent.capabilities.member_lifecycle.report import MemberLifecycleReport
from features.agent.report_helpers import make_event_report
from elbow_helper.infrastructure.ai import AgentStep, AgentUsage
from features.agent.engine.test_agent_plan_flow import _context, _Model, _Session, _model_step, _plan


def planned(identity, capability, arguments, *, reply=None):
    plan = _plan([{"id": identity, "capability": capability,
                   "arguments": arguments, "depends_on": []}])
    if reply is not None:
        plan["reply"] = reply
    return replace(_model_step(plan), usage=AgentUsage(1000, 100))


def unpack(items):
    return [dict(zip(items["columns"], row)) for row in items["rows"]] if isinstance(items, dict) else items


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

    async def test_original_count_recovers_from_404_with_reused_or_new_step_id(self):
        async def fetch(path, **options):
            return SimpleNamespace(ok=path.endswith("%23Q0"), status=200 if path.endswith("%23Q0") else 404,
                                   payload={"tag": "#Q0", "name": "Example clan", "members": 50})
        for corrected_id in ("clan", "resolved_clan"):
            with self.subTest(corrected_id=corrected_id):
                context = self.context(fetch)
                session = _Session([
                    planned("clan", "read_clash", {"kind": "clan", "tags": ["#P0"]}),
                    planned(corrected_id, "read_clash", {"kind": "clan", "tags": ["#Q0"]}),
                    AgentStep("49, full!", (), AgentUsage(1000, 100)),
                ], [])
                answer = await AgentService(_Model(session)).answer(question="Member count?", local_context="", context=context)
                self.assertEqual(answer, "50")
                self.assertEqual(len(session.calls), 2)
                self.assertEqual(context.bot.clash_client.get.await_count, 2)
                delivery = AgentDeliveryMixin()
                delivery.bot = context.bot
                await delivery.send_response(context.source_message, answer, None, context=context)
                self.assertEqual(context.source_message.reply.await_args.args[0], "50")

    async def test_explicit_single_clan_count_uses_one_read_and_no_model(self):
        context = self.context(lambda *_args, **_options: SimpleNamespace(
            ok=True, status=200, payload={"tag": "#Q0", "members": 50, "memberList": []}))
        model = SimpleNamespace(create_agent_session=Mock(side_effect=AssertionError("Unneeded model")))
        answer = await AgentService(model).answer(question="How many members in #Q0 currently?", local_context="", context=context)
        self.assertEqual(answer, "50")
        context.bot.clash_client.get.assert_awaited_once()
        model.create_agent_session.assert_not_called()
        for question in ("How many members in #Q0 have 300 trophies?", "How many members in #Q0 last month?",
                         "How many members in #Q0 and #P0?", "Should we raise the member count for #Q0?"):
            self.assertIsNone(clan_count_tag(question))

    def test_count_subject_and_successful_conflicts(self):
        def observation(tag, value):
            return {"source": "clash_api", "kind": "clan", "items": [{"tag": tag, "status": "ok", "data": {"members": value}}]}
        good = observation("#Q0", 50)
        self.assertEqual(narrow_answer("How many members in #Q0?", {"one": good, "duplicate": good,
                                                                     "unrelated": observation("#P0", 40)}), "50")
        with self.assertRaises(GroundingError):
            narrow_answer("How many members in #Q0?", {"one": good, "conflict": observation("#Q0", 49)})
        named = observation("#Q0", 50)
        named["items"][0]["data"]["name"] = "Example clan"
        self.assertEqual(narrow_answer("How many members in Example clan?", {"one": named}), "50")
        with self.assertRaises(GroundingError):
            narrow_answer("How many members in Different clan?", {"one": named})

    async def test_large_profiles_keep_every_ranked_league_in_model_and_retained_views(self):
        tags = ["#P" + str(index).replace("1", "Y").replace("3", "L").replace("4", "Q").replace("5", "V").replace("6", "G").replace("7", "R") for index in range(30)]
        def fetch(path, **options):
            tag = "#" + path.split("%23")[1]
            return SimpleNamespace(ok=True, status=200, payload={
                "tag": tag, "name": "Example " + tag, "league": {"name": "Silver III"},
                "leagueTier": {"id": 105000033, "name": "Electro League 33"},
                "trophies": 0, "bestTrophies": 6000,
                "troops": [{"name": "unneeded_profile_marker", "detail": "x" * 2000}] * 50})
        context = self.context(fetch)
        session = _Session([planned("players", "read_clash", {"kind": "player", "tags": tags, "detail": "full"}),
                            AgentStep("Checked the ranked leagues.", (), AgentUsage(1000, 100))], [])
        await AgentService(_Model(session)).answer(question="Read the player leagues.", local_context="", context=context)
        payload = json.loads(session.calls[1][0][0].content)["results"]["players"]
        items = unpack(payload["items"])
        self.assertEqual({item["tag"] for item in items}, set(tags))
        self.assertTrue(payload["truncated"])
        for item in items:
            self.assertEqual(item["data"]["leagueTier"]["name"], "Electro League 33")
            self.assertEqual(item["data"]["league"]["name"], "Silver III")
        evidence = json.loads(context.state.evidence[0])
        self.assertNotIn("unneeded_profile_marker", evidence["model_result"])
        turn = ConversationTurn("{}", frozenset(), record=ConversationRecord(
            100, 41, "2026-10-01T00:00:00+00:00", "Read player leagues", "Checked.", "Checked.", "",
            tuple(context.state.evidence), (), (), True))
        followup = replace(context, state=AgentTurnState(authorized_history=(turn,)))
        compiled = compile_context(question="Those same accounts, ranked league only.", local_context="", context=followup, tools=())
        self.assertNotIn("unneeded_profile_marker", compiled.prompt)
        self.assertIn("Electro League 33", compiled.prompt)

    async def test_cohort_leagues_count_members_across_accounts_and_page_without_extra_candidates(self):
        def fetch(path, **options):
            name = "Legend III" if path.endswith(("%23P0", "%23Q0")) else "Dragon League 30"
            return SimpleNamespace(ok=True, status=200, payload={"leagueTier": {"id": 105000034, "name": name},
                                   "league": {"name": "Silver III"}, "trophies": 0, "bestTrophies": 6000})
        context = self.context(fetch)
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
        second = await tool.handler(context, {"cohort_report_id": "cohort", "league": "Legend", "offset": first["next_offset"]})
        self.assertEqual(first["member_ids"], [101, 102])
        self.assertEqual(first["account_count"], 3)
        rows = first["accounts"] + second["accounts"]
        self.assertEqual({row["player_tag"] for row in rows if row["matches"]}, {"#P0", "#Q0"})
        self.assertEqual({row["member_id"] for row in rows if row["matches"]}, {101, 102})
        self.assertFalse(first["complete"])
        self.assertIsNone(second["next_offset"])
        self.assertEqual(context.bot.clash_client.get.await_count, 3)
        self.assertNotIn(103, context.account_links.get_links_for_members.call_args.args[0])
        complete = await tool.handler(context, {"cohort_report_id": "cohort", "league": "Legend League"})
        self.assertTrue(complete["complete"])
        self.assertEqual(complete["matching_members_in_page"], 2)
        self.assertEqual(complete["matching_accounts_in_page"], 2)
        self.assertEqual(context.bot.clash_client.get.await_count, 3)

    async def test_optional_failed_reaction_still_delivers_planned_text_in_one_round(self):
        events = []
        context = self.context(lambda *_a, **_kw: None)
        async def react(_emoji):
            events.append("reaction")
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "denied")
        context.source_message.add_reaction = AsyncMock(side_effect=react)
        context.source_message.reply.side_effect = lambda *a, **kw: events.append("text") or SimpleNamespace(id=501)
        session = _Session([planned("reaction", "react_to_request", {"emoji": "👍"}, reply="Still here.")], [])
        answer = await AgentService(_Model(session)).answer(question="u up?", local_context="", context=context)
        self.assertEqual(answer, "Still here.")
        delivery = AgentDeliveryMixin()
        delivery.bot = context.bot
        await delivery.send_response(context.source_message, answer, None, context=context)
        self.assertEqual(events, ["reaction", "text"])
        self.assertEqual(len(session.calls), 1)
        result = json.loads(context.state.evidence[-1])["result"]
        self.assertEqual(json.loads(result)["cause"], "permission_denied")

    async def test_join_cohort_filters_discord_join_dates_before_resolving_accounts(self):
        context = self.context(lambda *_a, **_kw: SimpleNamespace(ok=True, status=200, payload={
            "name": "Example", "leagueTier": {"name": "Legend III"}, "trophies": 0, "bestTrophies": 0}))
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

    async def test_planned_trial_report_and_leagues_cover_members_beyond_the_first_report_page(self):
        def fetch(path, **options):
            tier = "Legend III" if path.endswith(("%23P0", "%23P2", "%23Q0", "%23Q2")) else "Dragon League 30"
            return SimpleNamespace(ok=True, status=200, payload={"leagueTier": {"name": tier},
                                   "league": {"name": "Silver III"}, "trophies": 0})
        context = self.context(fetch)
        members = list(range(101, 132))
        links = {member: [{"player_tag": "#L" + str(member).replace("1", "Y").replace("3", "L").replace("4", "Q").replace("5", "V").replace("6", "G").replace("7", "R")}]
                 for member in members}
        links[101] = [{"player_tag": "#P0"}, {"player_tag": "#P2"}]
        links[102] = [{"player_tag": "#Q0"}]
        links[131] = [{"player_tag": "#Q2"}]
        links[999] = [{"player_tag": "#P8"}]
        queries = RecruitmentQueries(lambda: {str(200 + index): {
            "applicant_id": member, "start": "2026-10-01T00:00:00+00:00", "days": 7,
        } for index, member in enumerate(members)}, clock=lambda: datetime(2026, 10, 4, tzinfo=timezone.utc))
        channels = {91: context.source_message.channel}
        for identifier in range(200, 231):
            channels[identifier] = SimpleNamespace(id=identifier, guild=context.guild,
                type=discord.ChannelType.text, category_id=RECRUITMENT_TICKET_CATEGORY,
                permissions_for=lambda _: SimpleNamespace(view_channel=True, read_message_history=True))
        context.guild.get_channel_or_thread = channels.get
        context = replace(context, recruitment_queries=queries, account_links=SimpleNamespace(
            get_links_for_members=Mock(side_effect=lambda ids: {value: links[value] for value in ids})))
        plan = _plan([
            {"id": "trials", "capability": "read_active_recruitment_trials", "arguments": {}, "depends_on": []},
            {"id": "leagues", "capability": "read_cohort_leagues", "arguments": {
                "cohort_report_id": {"step": "trials", "path": ["report_id"]}, "league": "Legend",
            }, "depends_on": ["trials"]},
        ])
        session = _Session([replace(_model_step(plan), usage=AgentUsage(1000, 100)),
                            AgentStep("3 members, across 4 matching accounts.", (), AgentUsage(1000, 100))], [])
        answer = await AgentService(_Model(session)).answer(question="How many recruits are in Legend league?", local_context="", context=context)
        results = json.loads(session.calls[1][0][0].content)["results"]
        self.assertEqual(len(unpack(results["trials"]["trials"])), 25)
        self.assertEqual(results["leagues"]["member_count"], 31)
        self.assertTrue(results["leagues"]["complete"])
        self.assertEqual(results["leagues"]["matching_members_in_page"], 3)
        self.assertEqual(results["leagues"]["matching_accounts_in_page"], 4)
        self.assertEqual(context.bot.clash_client.get.await_count, 32)
        self.assertEqual(len(session.calls), 2)
        delivery = AgentDeliveryMixin()
        delivery.bot = context.bot
        await delivery.send_response(context.source_message, answer, None, context=context)
        self.assertEqual(context.source_message.reply.await_args.args[0], answer)

    async def test_unused_saved_report_manifest_does_not_import_its_restricted_provenance(self):
        context = self.context(lambda *_a, **_kw: None)
        report = make_event_report("example-report", "2026-09-17")
        context.state.reports[report.report_id] = report
        context.state.report_sources[report.report_id] = frozenset({91})
        context.state.report_access_requirements[report.report_id] = frozenset({"lead"})
        compiled = compile_context(question="What's a clan war?", local_context="", context=context, tools=())
        self.assertFalse(context.state.required_access)
        manifest = json.loads(compiled.prompt.split("<available_reports>\n")[1].split("\n</available_reports>")[0])[0]
        self.assertEqual(set(manifest), {"report_id", "kind", "requires_read", "filter_fields"})
        self.assertTrue(manifest["requires_read"])


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

    async def test_missing_history_date_is_repaired_once_and_identical_invalid_retry_stops(self):
        for repeat in (False, True):
            with self.subTest(repeat=repeat):
                context = self.context(lambda *_a, **_kw: SimpleNamespace(ok=True, status=200, payload={
                    "name": "Example", "leagueTier": {"name": "Dragon League 30"}}))
                async def page(**arguments):
                    return SimpleNamespace(**arguments, messages=(), reached_window_start=True, next_before_id=None)
                search = SimpleNamespace(history_page=AsyncMock(side_effect=page))
                context = replace(context, message_search=search)
                context.source_message.id = discord.utils.time_snowflake(context.source_message.created_at, high=True)
                arguments = {"channel_id": 91}
                first = _plan([
                    {"id": "account", "capability": "read_clash", "arguments": {"kind": "player", "tags": ["#P0"]}, "depends_on": []},
                    {"id": "history", "capability": "read_discord_channel_history", "arguments": arguments, "depends_on": []},
                ])
                corrected = arguments if repeat else {**arguments, "after": "2025-12-01"}
                session = _Session([
                    replace(_model_step(first), usage=AgentUsage(1000, 100)),
                    planned("history", "read_discord_channel_history", corrected),
                    AgentStep("Account checked. History could not be read." if repeat else "Account checked. No messages in that window.", (), AgentUsage(1000, 100)),
                ], [])
                answer = await AgentService(_Model(session)).answer(question="Analyze the example case", local_context="", context=context)
                errors = json.loads(session.calls[1][0][0].content)["results"]
                self.assertIn("after", errors["history"]["argument_schema"]["required"])
                self.assertEqual(errors["history"]["cause"], "invalid_arguments")
                self.assertEqual(errors["account"]["items"][0]["data"]["leagueTier"]["name"], "Dragon League 30")
                self.assertIn("Account checked.", answer)
                self.assertEqual(search.history_page.await_count, 0 if repeat else 1)
                self.assertEqual(len(session.calls), 3)
                if repeat:
                    self.assertFalse(session.calls[-1][1])


    def test_followup_keeps_resolved_accounts_cohort_metric_and_exact_correction(self):
        correction = "I asked about competitive promotion, not CWL eligibility. Use regular wars and same TH or +1 triples."
        evidence = json.dumps({"call_id": "cohort", "tool": "read_cohort_leagues", "result_complete": True,
            "result": json.dumps({"source": "cohort_ranked_leagues", "member_ids": [101, 102],
                                  "accounts": [{"member_id": 101, "player_tag": "#P0", "ranked_league": "Legend III"}]})})
        turns = tuple(ConversationTurn("{}", frozenset({91}), record=ConversationRecord(
            index, 41, "2026-10-01T00:00:00+00:00", question, "Checked.", "Checked.",
            "unrelated_old_window_marker" * 1000, (evidence,) if index == 1 else (), (), (), True))
            for index, question in enumerate(("Review this cohort.", correction), 1))
        context = _context()
        context.state.authorized_history = turns
        compiled = compile_context(question="Does he meet that bar?", local_context="", context=context, tools=())
        self.assertIn(correction, compiled.prompt)
        self.assertIn("#P0", compiled.prompt)
        self.assertIn("cohort_ranked_leagues", compiled.prompt)
        self.assertNotIn("unrelated_old_window_marker", compiled.prompt)
        self.assertEqual(context.state.source_channels, {91})
        fresh = replace(context, state=AgentTurnState(authorized_history=turns))
        scoped = compile_context(question="What's another member's league?", local_context="", context=fresh, tools=())
        self.assertNotIn(correction, scoped.prompt)
        self.assertFalse(fresh.state.required_access)
