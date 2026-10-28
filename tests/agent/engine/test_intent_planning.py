"""Synthetic plans exercise seasonal CWL cohorts and retained hibernation ranking."""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from elbow_helper.configuration.channels import HIBERNATION_LOG
from elbow_helper.configuration.clans import CLANS
from elbow_helper.agent.capabilities.hibernation.reads import hibernation_tools
from elbow_helper.agent.datasets.clash import clash_tools
from elbow_helper.agent.datasets.guide import DataGuide
from elbow_helper.agent.engine.service import AgentService
from elbow_helper.agent.knowledge.store import KnowledgeCatalog, KnowledgeSection
from elbow_helper.agent.knowledge.tools import knowledge_tools
from elbow_helper.agent.research.members import member_tools
from elbow_helper.features.hibernation.queries import HibernationQueries
from agent.engine import test_request_cost as cost
from agent.engine.test_agent_plan_flow import _context, _model_step, _plan, _step, _Model, _Session


def step(identity, capability, arguments, depends_on=()):
    return {**_step(identity, arguments, list(depends_on)), "capability": capability}


class IntentPlanningTests(unittest.IsolatedAsyncioTestCase):
    def test_cwl_cohort_guidance_survives_recruitment_scoping(self):
        guide = DataGuide(None, sources=())
        public = guide.for_levels(set())
        recruitment = guide.for_levels({"recruitment"})
        for text in (public, recruitment):
            self.assertIn("For clans configured for seasonal cohort lookups, resolve the season's assigned roster", text)
            self.assertIn("Read player.warStars for cumulative stars", text)
        self.assertIn("Recruitment: state.recruitment_trials", recruitment)
        self.assertIn("not a new accept/decline stage", recruitment)
        self.assertNotIn("Recruitment: state.recruitment_trials", public)

    def cwl_flow(self, *, available):
        clan = CLANS["BE4"]
        catalog = KnowledgeCatalog("now", (KnowledgeSection(
            "Brown Elbow IV (BE4)",
            "BE4 is CWL-only. Its real players live in Hellbow, BES or BE1 between seasons; "
            "the in-game roster contains eight placeholder accounts.",
            file_name="clan-profiles.md", always_loaded=True,
        ),)).prepare_prompts()
        group = {"season": "2026-10", "clans": [
            {"tag": "#Q0", "members": [{"tag": "#Q2", "name": "Opponent"}]},
            {"tag": clan.tag, "members": [{"tag": "#P0", "name": "Player A"},
                                          {"tag": "#P2", "name": "Player B"}]},
        ]}
        payloads = {
            "/clans/%232YLRRYLC8/currentwar/leaguegroup": group,
            "/players/%23P0": {"tag": "#P0", "name": "Player A", "warStars": 4500,
                                "clan": {"tag": CLANS["BEH"].tag}},
            "/players/%23P2": {"tag": "#P2", "name": "Player B", "warStars": 3200,
                                "clan": {"tag": CLANS["BES"].tag}},
        }
        async def get(path, **kwargs):
            self.assertIn(path, payloads)  # No parked accounts or opponent profiles.
            return SimpleNamespace(ok=available, status=200 if available else 404,
                                   payload=payloads[path] if available else None)
        client = SimpleNamespace(configured=True, get=AsyncMock(side_effect=get))
        context = replace(_context(), bot=SimpleNamespace(clash_client=client),
                          knowledge_store=SimpleNamespace(load=lambda: catalog))
        plans = [_model_step(_plan([step("profile", "search_approved_knowledge", {
            "titles": ["Brown Elbow IV (BE4)"],
        })])), _model_step(_plan([step("season", "read_clash", {
            "kind": "cwl_group", "tags": ["BE4"],
        })]))]
        if available:
            plans.append(_model_step(_plan([step("stars", "read_clash", {
                "kind": "player", "tags": {"step": "season", "path": [
                    "items", 0, "data", "clans", 1, "members", "*", "tag"]},
                "fields": ["tag", "name", "warStars"],
            })])))
            answer = "Player A has the most cumulative war stars in BE4's October CWL roster: 4,500."
        else:
            answer = "BE4's CWL roster isn't available here, so I can't rank its CWL players by war stars."
        session = _Session([*plans, cost.response(answer)], [])
        model = _Model(session)
        registry = {tool.definition.name: tool for tool in (*knowledge_tools(), *clash_tools())}
        with patch("elbow_helper.agent.engine.service.build_agent_tools", return_value=registry):
            flow, _ = AgentService(model)._start_flow(
                question="and in BE4?", context=context,
                local_context="Asker: Who has the most war stars in Hellbow?\nHelper: Player C, 5,000.",
                conversation_history="",
            )
        return flow, model, client, answer

    async def test_cwl_only_followup_reads_season_roster_then_cumulative_player_stars(self):
        flow, model, client, answer = self.cwl_flow(available=True)
        self.assertIn("For a CWL-only clan, use the relevant", model.request["system_prompt"])
        self.assertNotIn("eight placeholder accounts", model.request["system_prompt"])
        self.assertEqual(await flow.run(), answer)
        self.assertEqual(client.get.await_count, 3)
        self.assertEqual(flow.runner.tool_calls, 3)

    async def test_unavailable_cwl_cohort_does_not_fall_back_to_parked_accounts(self):
        flow, _, client, answer = self.cwl_flow(available=False)
        self.assertEqual(await flow.run(), answer)
        self.assertEqual(client.get.await_count, 1)

    async def test_small_hibernation_page_can_feed_full_join_ranking_in_one_plan(self):
        context = _context()
        old = SimpleNamespace(id=101, display_name="Old nickname", name="old_username", bot=False,
                              roles=[], joined_at=datetime(2023, 1, 12, tzinfo=timezone.utc))
        recent = SimpleNamespace(id=102, display_name="Recent nickname", name="recent_username", bot=False,
                                 roles=[], joined_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
        context.guild.members.extend([old, recent])
        members = {m.id: m for m in context.guild.members}
        context.guild.get_member = members.get
        log = SimpleNamespace(id=HIBERNATION_LOG, guild=context.guild,
                              permissions_for=context.source_message.channel.permissions_for)
        context.guild.get_channel_or_thread = lambda i: log if i == HIBERNATION_LOG else context.source_message.channel
        context = replace(context, private_pass=True, hibernation_queries=HibernationQueries(lambda: {
            "101": {"hibernation_date": "<t:1700000000:F>"},
            "102": {"hibernation_date": "<t:1750000000:F>"},
        }))
        registry = {tool.definition.name: tool for tool in (*hibernation_tools(), *member_tools())}
        flow, session, model, _ = cost.RequestCostTests.flow(self, [
            _model_step({**_plan([
                step("active", "read_active_hibernation", {"limit": 1}),
                step("joins", "read_discord_members", {
                    "hibernation_report_id": {"step": "active", "path": ["report_id"]},
                    "sort": "oldest_first", "limit": 1,
                }, ["active"]),
            ]), "reply": "Pulling the 10 oldest by Discord join date."}),
            cost.response("Old nickname joined on 12 Jan 2023."),
        ], question="can't you get their join date from discord? if not, sort by longest hibernating",
            context=context, registry=registry)
        self.assertEqual(await flow.run(), "Old nickname joined on 12 Jan 2023.")
        results = session.calls[-1][0][0].content
        self.assertIn("Old nickname", results)
        self.assertNotIn("old_username", results)
        self.assertIn("Equal counts do not prove equal member IDs", model.request["system_prompt"])
        self.assertEqual(flow.runner.tool_calls, 2)
